#!/usr/bin/env python3
"""Simulated ACH Operator: the gateway of the distributed simulator (HTTP).

Banks talk only to this service. For each submitted file it calls the validation
service (edits) and the settlement service (dates and windows), splits entries
by RDFI into per-bank mailboxes, and hands files for simulated banks (mode "auto")
to that bank's RDFI service. RDFI services send their returns and NOCs back here
as files of their own. Files for "mailbox" banks wait for the bank's client.

    POST /v1/files                 submit a Nacha file (body = file text)
    GET  /v1/files/<file_id>       status of a submitted file
    GET  /v1/mailbox               list files waiting for the caller's bank
    GET  /v1/mailbox/<name>        download one file
    POST /v1/mailbox/<name>/ack    confirm pickup (moves it to picked_up/)
    GET  /v1/events?limit=N        recent processing events (JSON lines also on disk)
    GET  /v1/health

Auth: header  X-API-Key: <key from participants.py>
Simulated clock: optional header  X-Sim-Time: 2026-11-02T09:15:00-05:00

    python3 -m ach_sim.operator_server --port 8770 --data-dir sim_data \\
        --validation-url http://127.0.0.1:8771 --settlement-url http://127.0.0.1:8772 \\
        --rdfi 520000027=http://127.0.0.1:8773 --rdfi 530000033=http://127.0.0.1:8774
Every option defaults to the ports above, so `python3 -m ach_sim.cluster` is the
easy way to start the whole set.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from . import nacha, rdfi, telemetry
from .participants import OPERATOR_NAME, OPERATOR_ROUTING, PARTICIPANTS
from .service import DEFAULT_PORTS, HOST, TracedHandler, call, default_url, run_service
from .settlement import ET

SERVICE_NAME = "ach-operator"
MODIFIERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
SUBMIT_REASONS = {200: "accepted", 401: "unknown_api_key", 403: "origin_mismatch",
                  409: "duplicate", 422: "failed_edits", 503: "dependency_unavailable"}
# Event fields echoed into the log message; the full event goes in the log attributes.
EVENT_LOG_KEYS = ("file_id", "submitter", "rdfi", "bank", "to", "trace", "outcome", "code",
                  "mailbox_file", "errors")
log = logging.getLogger("ach_sim.operator")


def bank_name(rt: str) -> str:
    return OPERATOR_NAME if rt == OPERATOR_ROUTING else PARTICIPANTS.get(rt, {}).get("name", rt)


def default_rdfi_urls() -> dict:
    return {rt: default_url(rt) for rt, p in PARTICIPANTS.items() if p["mode"] == "auto"}


class DependencyError(Exception):
    """A downstream service could not be reached or answered with an error."""


class Operator:
    def __init__(self, data_dir: Path, validation_url: str | None = None,
                 settlement_url: str | None = None, rdfi_urls: dict | None = None):
        self.data = Path(data_dir)
        (self.data / "received").mkdir(parents=True, exist_ok=True)
        for rt in PARTICIPANTS:
            (self.data / "mailbox" / rt / "picked_up").mkdir(parents=True, exist_ok=True)
        self.validation_url = validation_url or default_url("validation")
        self.settlement_url = settlement_url or default_url("settlement")
        self.rdfi_urls = rdfi_urls if rdfi_urls is not None else default_rdfi_urls()
        self.files: dict = {}
        self.seen_keys: set = set()
        self.lock = threading.Lock()
        self.events_path = self.data / "events.jsonl"
        # Resume numbering from an existing data dir so a restart never reuses
        # a file ID or mailbox file name (and so never overwrites earlier files).
        self.counter = max((int(m.group(1)) for m in (
            re.search(r"-(\d+)\.ach$", p.name) for p in (self.data / "received").glob("F*.ach"))
            if m), default=0)
        self.out_counter = max((int(m.group(1)) for m in (
            re.match(r"\d{8}T\d{4}_(\d+)_", p.name)
            for p in (self.data / "mailbox").glob("*/**/*.ach")) if m), default=0)

    # -- helpers ------------------------------------------------------------
    def event(self, kind: str, **details):
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "event": kind, **details, **telemetry.current_ids()}
        with self.events_path.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        brief = " ".join(f"{k}={details[k]}" for k in EVENT_LOG_KEYS if k in details)
        log.log(logging.WARNING if kind in ("file_rejected", "rdfi_unavailable")
                else logging.INFO, "%s %s", kind, brief,
                extra={"attrs": {"ach.event": kind,
                                 **{f"ach.{k}": v for k, v in details.items()}}})
        return rec

    def deliver(self, rt: str, f: nacha.AchFile, label: str) -> str:
        self.out_counter += 1
        f.file_id_modifier = MODIFIERS[self.out_counter % len(MODIFIERS)]
        name = f"{f.created:%Y%m%dT%H%M}_{self.out_counter:05d}_{label}.ach"
        (self.data / "mailbox" / rt / name).write_text(f.to_text())
        telemetry.MAILBOX_OPS.add(1, {"ach.operation": "deliver", "ach.bank.name": bank_name(rt)})
        return name

    @staticmethod
    def caller(api_key: str):
        for rt, p in PARTICIPANTS.items():
            if p["api_key"] == api_key:
                return rt
        return None

    def _downstream(self, what: str, method: str, url: str, **kwargs):
        try:
            status, resp = call(method, url, **kwargs)
        except OSError as exc:
            raise DependencyError(f"{what} unavailable at {url}: {exc}") from exc
        if status != 200 or not isinstance(resp, dict):
            raise DependencyError(f"{what} answered HTTP {status}: {resp}")
        return resp

    # -- submission ---------------------------------------------------------
    def submit(self, text: str, api_key: str, sim_time: datetime) -> tuple[int, dict]:
        caller = self.caller(api_key)
        with telemetry.span("ach.file.submit", **{
                "ach.submitter.routing": caller or "unknown",
                "ach.submitter.name": bank_name(caller) if caller else "unknown",
                "ach.file.bytes": len(text), "ach.sim_time": sim_time.isoformat()}) as s:
            start = time.perf_counter()
            dispatch = []
            if caller is None:
                code, resp = 401, {"status": "rejected", "errors": ["unknown API key"]}
                log.warning("file submission with an unknown API key rejected")
            else:
                with self.lock:
                    code, resp, dispatch = self._submit(text, caller, sim_time)
            # Simulated RDFIs are called outside the lock: they answer by submitting
            # their own return file, which comes back into submit() on another thread.
            for rt, file_id, forwarded in dispatch:
                self._dispatch(rt, file_id, forwarded, sim_time, resp)
            result = resp["status"]
            reason = SUBMIT_REASONS.get(code, str(code))
            s.set_attributes({"ach.file.id": resp.get("file_id", ""), "ach.file.status": result,
                              "ach.file.reject_reason": "" if code == 200 else reason})
            if code != 200:
                s.add_event("ach.file.rejected", {"ach.errors": resp.get("errors", [])})
            if code >= 500:
                telemetry.mark_error(s, "; ".join(resp.get("errors", [])))
            attrs = {"ach.result": result, "ach.reason": reason,
                     "ach.submitter": bank_name(caller) if caller else "unknown"}
            telemetry.FILES.add(1, attrs)
            telemetry.FILE_DURATION.record(time.perf_counter() - start, attrs)
            return code, resp

    def _submit(self, text: str, caller: str, now: datetime):
        """Edit, settle and route one file. Returns (status, body, dispatch) where
        dispatch lists (rdfi, file_id, forwarded text) for the simulated RDFIs."""
        self.counter += 1
        file_id = f"F{now:%Y%m%d}-{self.counter:04d}"
        (self.data / "received" / f"{file_id}.ach").write_text(text)

        def reject(status, errs, warnings=None):
            self.event("file_rejected", file_id=file_id, submitter=caller,
                       sim_time=now.isoformat(), errors=errs)
            self.files[file_id] = {"status": "rejected", "errors": errs}
            body = {"file_id": file_id, "status": "rejected", "errors": errs}
            if warnings is not None:
                body["warnings"] = warnings
            return status, body, []

        try:
            res = self._downstream("validation service", "POST",
                                   f"{self.validation_url}/v1/validate", body=text)
        except DependencyError as exc:
            return reject(503, [str(exc)])
        if not res["ok"]:
            return reject(422, res["errors"], res["warnings"])
        f = nacha.parse(text)
        if f.immediate_origin != caller:
            return reject(403, [f"immediate origin {f.immediate_origin} does not match the "
                                f"authenticated bank {caller}"])
        if f.immediate_destination != OPERATOR_ROUTING:
            return reject(422, [f"immediate destination must be the operator ({OPERATOR_ROUTING})"])
        if f.file_key in self.seen_keys:
            return reject(409, [f"duplicate file: {f.file_key} (same origin, creation date/time "
                                "and file ID modifier) was already accepted"])

        # Settlement date and window per batch, from the settlement service
        eligible = {b.batch_number: all(e.amount <= nacha.SAME_DAY_LIMIT_CENTS
                                        for e in b.entries) for b in f.batches}
        try:
            settled = self._downstream("settlement service", "POST",
                                       f"{self.settlement_url}/v1/settle", json_body={
                                           "submitted_at": now.isoformat(),
                                           "batches": [{"batch": b.batch_number,
                                                        "effective_date": b.effective_date,
                                                        "same_day_eligible":
                                                            eligible[b.batch_number]}
                                                       for b in f.batches]})
        except DependencyError as exc:
            return reject(503, [str(exc)])
        self.seen_keys.add(f.file_key)
        windows = {x["batch"]: x for x in settled["batches"]}
        batch_info = []
        for b in f.batches:
            w = windows[b.batch_number]
            b.settlement_date = nacha.julian_day(
                datetime.fromisoformat(w["settlement_date"]).date())
            d, c = b.totals()
            batch_info.append({"batch": b.batch_number, "sec": b.sec_code,
                               "company": b.company_name, "entries": len(b.entries),
                               "debit_cents": d, "credit_cents": c,
                               "effective_date": b.effective_date,
                               "settlement_date": w["settlement_date"], "window": w["window"],
                               "same_day_eligible": eligible[b.batch_number]})
            for direction, cents in (("debit", d), ("credit", c)):
                if cents:
                    telemetry.AMOUNT.add(cents / 100, {"ach.sec": b.sec_code,
                                                       "ach.direction": direction,
                                                       "ach.window": w["window"]})

        self.event("file_accepted", file_id=file_id, submitter=caller, key=f.file_key,
                   sim_time=now.isoformat(),
                   **f.summary(), batches_detail=batch_info, warnings=res["warnings"])

        # Route entries to RDFIs
        per_rdfi: dict = {}
        r13 = []
        for b in f.batches:
            for e in b.entries:
                if e.rdfi_routing not in PARTICIPANTS:
                    r13.append((b, e, "return", "R13", "RDFI NOT A SIM PARTICIPANT"))
                    continue
                per_rdfi.setdefault(e.rdfi_routing, {}).setdefault(id(b), (b, []))[1].append(e)

        delivered, dispatch = [], []
        for rt, groups in per_rdfi.items():
            n_entries = sum(len(x[1]) for x in groups.values())
            with telemetry.span("ach.rdfi.route", **{
                    "ach.file.id": file_id, "ach.rdfi.routing": rt,
                    "ach.rdfi.name": bank_name(rt), "ach.rdfi.mode": PARTICIPANTS[rt]["mode"],
                    "ach.entries": n_entries}) as rs:
                out = nacha.AchFile(immediate_destination=rt, immediate_origin=OPERATOR_ROUTING,
                                    destination_name=PARTICIPANTS[rt]["name"],
                                    origin_name=OPERATOR_NAME, created=now.astimezone(ET))
                for b, entries in groups.values():
                    nb = out.add_batch(company_name=b.company_name, company_id=b.company_id,
                                       sec_code=b.sec_code, entry_description=b.entry_description,
                                       effective_date=b.effective_date,
                                       odfi_routing=b.odfi_routing,
                                       service_class=b.service_class,
                                       descriptive_date=b.descriptive_date,
                                       discretionary=b.discretionary,
                                       settlement_date=b.settlement_date)
                    nb.entries = list(entries)
                name = self.deliver(rt, out, f"from_{caller}")
                rs.set_attribute("ach.mailbox.file", name)
                delivered.append({"rdfi": rt, "mailbox_file": name, "entries": n_entries})
                telemetry.ENTRIES_FORWARDED.add(n_entries, {"ach.rdfi.name": bank_name(rt)})
                self.event("entries_forwarded", file_id=file_id, rdfi=rt, mailbox_file=name,
                           traces=[e.trace_number for _, es in groups.values() for e in es])
                if PARTICIPANTS[rt]["mode"] == "auto":
                    dispatch.append((rt, file_id, out.to_text()))

        # Entries for banks outside the network come back from the operator as R13.
        if r13:
            odfi = r13[0][0].odfi_routing
            dest = odfi if odfi in PARTICIPANTS else caller
            for b, e, kind, code, data in r13:
                telemetry.ENTRY_OUTCOMES.add(1, {"ach.rdfi.name": OPERATOR_NAME,
                                                 "ach.outcome": kind, "ach.code": code,
                                                 "ach.sec": b.sec_code})
                self.event("operator_return", file_id=file_id, trace=e.trace_number, code=code)
            rf = rdfi.build_response_file(
                r13, rdfi_routing=OPERATOR_ROUTING, origin=OPERATOR_ROUTING,
                origin_name=OPERATOR_NAME, destination=dest,
                destination_name=PARTICIPANTS[dest]["name"], created=now.astimezone(ET))
            name = self.deliver(dest, rf, f"returns_from_{OPERATOR_ROUTING}")
            self.event("returns_delivered", file_id=file_id, to=dest, from_rdfi=OPERATOR_ROUTING,
                       mailbox_file=name,
                       items=[{"original_trace": e.trace_number, "code": code}
                              for _, e, _, code, _ in r13])

        ack = {"file_id": file_id, "status": "accepted", "received_at": now.isoformat(),
               "file_key": f.file_key, "totals": f.summary(), "batches": batch_info,
               "forwarded": delivered, "warnings": res["warnings"],
               "note": "Acceptance means the file passed operator edits. Returns and NOCs "
                       "arrive later in your mailbox."}
        self.files[file_id] = ack
        return 200, ack, dispatch

    def _dispatch(self, rt: str, file_id: str, text: str, now: datetime, ack: dict):
        """Hand a forwarded file to a simulated RDFI service and record its decisions."""
        url = self.rdfi_urls.get(rt)
        with telemetry.span("ach.rdfi.dispatch", **{
                "ach.file.id": file_id, "ach.rdfi.routing": rt,
                "ach.rdfi.name": bank_name(rt), "url.full": url or ""}) as s:
            try:
                if not url:
                    raise DependencyError(f"no RDFI service configured for {rt}")
                resp = self._downstream(f"RDFI service {bank_name(rt)}", "POST",
                                        f"{url}/v1/inbound", body=text,
                                        headers={"X-Sim-Time": now.isoformat(),
                                                 "X-File-Id": file_id})
            except DependencyError as exc:
                telemetry.mark_error(s, exc)
                self.event("rdfi_unavailable", file_id=file_id, rdfi=rt, errors=[str(exc)])
                ack.setdefault("warnings", []).append(
                    f"entries for {bank_name(rt)} were delivered to its mailbox, but its "
                    f"RDFI service did not process them: {exc}")
                return
            for d in resp["decisions"]:
                if d["kind"] == "received":
                    self.event("return_or_noc_received", file_id=file_id, bank=rt,
                               trace=d["trace"], code=d["code"])
                else:
                    self.event("rdfi_decision", file_id=file_id, rdfi=rt, trace=d["trace"],
                               amount_cents=d["amount_cents"], outcome=d["outcome"])
            rf = resp.get("returns_file")
            if rf:
                s.set_attribute("ach.returns_file.id", rf.get("file_id") or "")
                for fwd in ack.get("forwarded", []):
                    if fwd["rdfi"] == rt:
                        fwd["rdfi_returns_file"] = rf.get("file_id")

    # -- mailbox ------------------------------------------------------------
    def mailbox(self, rt: str) -> list:
        box = self.data / "mailbox" / rt
        return [{"name": p.name, "bytes": p.stat().st_size}
                for p in sorted(box.glob("*.ach"))]


def make_handler(op: Operator):
    class Handler(TracedHandler):
        server_version = "SimACHOperator/2.0"

        def _caller(self):
            rt = op.caller(self.headers.get("X-API-Key", ""))
            if rt is None:
                self.send(401, {"error": "unknown or missing X-API-Key"})
            return rt

        def handle_get(self):
            path = self.route_path
            if path == "/v1/health":
                return self.send(200, {"status": "ok", "service": SERVICE_NAME,
                                       "operator": OPERATOR_ROUTING})
            if path == "/v1/events":
                limit = 50
                if "limit=" in self.path:
                    limit = int(self.path.split("limit=")[1].split("&")[0])
                lines = op.events_path.read_text().splitlines() if op.events_path.exists() else []
                return self.send(200, [json.loads(x) for x in lines[-limit:]])
            rt = self._caller()
            if rt is None:
                return
            if path.startswith("/v1/files/"):
                fid = path.rsplit("/", 1)[1]
                if fid not in op.files:
                    return self.send(404, {"error": "unknown file id"})
                return self.send(200, op.files[fid])
            if path == "/v1/mailbox":
                return self.send(200, {"bank": rt, "files": op.mailbox(rt)})
            if path.startswith("/v1/mailbox/"):
                name = path.rsplit("/", 1)[1]
                p = op.data / "mailbox" / rt / name
                if not SAFE_NAME.match(name) or not p.exists():
                    return self.send(404, {"error": "no such file"})
                op.event("mailbox_download", bank=rt, mailbox_file=name)
                telemetry.MAILBOX_OPS.add(1, {"ach.operation": "download",
                                              "ach.bank.name": bank_name(rt)})
                return self.send(200, p.read_text(), "text/plain")
            self.send(404, {"error": "not found"})

        def handle_post(self):
            path = self.route_path
            body = self.body_text()
            if path == "/v1/files":
                code, resp = op.submit(body, self.headers.get("X-API-Key", ""), self.sim_time())
                return self.send(code, resp)
            m = re.match(r"^/v1/mailbox/([^/]+)/ack$", path)
            if m:
                rt = self._caller()
                if rt is None:
                    return
                name = m.group(1)
                src = op.data / "mailbox" / rt / name
                if not SAFE_NAME.match(name) or not src.exists():
                    return self.send(404, {"error": "no such file"})
                shutil.move(src, src.parent / "picked_up" / name)
                op.event("mailbox_ack", bank=rt, mailbox_file=name)
                telemetry.MAILBOX_OPS.add(1, {"ach.operation": "ack",
                                              "ach.bank.name": bank_name(rt)})
                return self.send(200, {"acknowledged": name})
            self.send(404, {"error": "not found"})

    return Handler


def serve(host=HOST, port=DEFAULT_PORTS["operator"], data_dir="sim_data", *,
          validation_url=None, settlement_url=None, rdfi_urls=None):
    op = Operator(Path(data_dir), validation_url, settlement_url, rdfi_urls)
    httpd = ThreadingHTTPServer((host, port), make_handler(op))
    return op, httpd


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORTS["operator"])
    ap.add_argument("--data-dir", default="sim_data")
    ap.add_argument("--validation-url", default=default_url("validation"))
    ap.add_argument("--settlement-url", default=default_url("settlement"))
    ap.add_argument("--rdfi", action="append", default=[], metavar="ROUTING=URL",
                    help="RDFI service for a simulated bank (repeatable; defaults per bank)")
    args = ap.parse_args()
    rdfi_urls = default_rdfi_urls()
    for item in args.rdfi:
        rt, _, url = item.partition("=")
        rdfi_urls[rt] = url
    _, httpd = serve(args.host, args.port, args.data_dir, validation_url=args.validation_url,
                     settlement_url=args.settlement_url, rdfi_urls=rdfi_urls)
    run_service(httpd, SERVICE_NAME,
                f"{SERVICE_NAME} {OPERATOR_ROUTING} listening on "
                f"http://{args.host}:{httpd.server_address[1]}  (data in {args.data_dir}/)")


if __name__ == "__main__":
    main()
