#!/usr/bin/env python3
"""Bank-side client for the simulated ACH Operator.

Outbound (origination):  validate a Nacha file locally, then transmit it.
Inbound (receiving):     pull files from the bank's mailbox, post entries to a
                         local ledger, and transmit returns/NOCs for any entry
                         that cannot be posted.

    python -m ach_sim.client send examples/01_ppd_payroll_credits.ach
    python -m ach_sim.client inbox
    python -m ach_sim.client receive --ledger examples/first_example_bank_ledger.json
    python -m ach_sim.client status F20261102-0001
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from . import nacha, rdfi, telemetry
from .service import call, default_url
from .participants import FIRST_EXAMPLE_BANK, OPERATOR_ROUTING, PARTICIPANTS
from .settlement import ET

DEFAULT_URL = default_url("operator")
DEFAULT_KEY = PARTICIPANTS[FIRST_EXAMPLE_BANK]["api_key"]
log = logging.getLogger("ach_sim.client")
SUBMIT_REASON = {401: "unknown_api_key", 403: "origin_mismatch", 409: "duplicate",
                 422: "failed_edits"}


class OperatorClient:
    def __init__(self, url=DEFAULT_URL, api_key=DEFAULT_KEY, sim_time: str | None = None):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.sim_time = sim_time
        self.routing = next((rt for rt, p in PARTICIPANTS.items() if p["api_key"] == api_key), None)
        self.bank_name = PARTICIPANTS.get(self.routing, {}).get("name", "unknown")

    def _req(self, method, path, body: str | None = None):
        headers = {"X-API-Key": self.api_key}
        if self.sim_time:
            headers["X-Sim-Time"] = self.sim_time
        return call(method, self.url + path, body=body, headers=headers)

    # -- outbound -----------------------------------------------------------
    def send_file(self, text: str, precheck: bool = True):
        with telemetry.span("ach.client.send_file", **{
                "ach.bank.routing": self.routing, "ach.bank.name": self.bank_name,
                "ach.file.bytes": len(text), "ach.precheck": precheck}) as s:
            if precheck:
                with telemetry.step("precheck", span_name="ach.client.precheck", logger=log,
                                    **{"ach.bank.name": self.bank_name}) as ps:
                    res = nacha.validate(text)
                    ps.span.set_attribute("ach.validation.errors", len(res.errors))
                    ps.detail["errors"] = len(res.errors)
                    if not res.ok:
                        ps.reject("failed_local_validation")
                if not res.ok:
                    s.set_attribute("ach.file.status", "not sent")
                    telemetry.mark_error(s, "failed local pre-transmission validation")
                    log.warning("file not sent: failed local validation: %s", "; ".join(res.errors),
                                extra={"attrs": {"ach.bank.name": self.bank_name,
                                                 "ach.validation.errors": res.errors}})
                    return None, {"status": "not sent", "errors": res.errors,
                                  "note": "failed local pre-transmission validation"}
            with telemetry.step("transmit", logger=log,
                                **{"ach.bank.name": self.bank_name}) as ts:
                status, resp = self._req("POST", "/v1/files", text)
                info = resp if isinstance(resp, dict) else {}
                ts.detail.update(http_status=status, file_id=info.get("file_id", ""))
                if status >= 500:
                    ts.fail(f"operator answered HTTP {status}")
                elif status != 200:
                    ts.reject(SUBMIT_REASON.get(status, f"http_{status}"))
            s.set_attributes({"ach.file.id": info.get("file_id", ""),
                              "ach.file.status": info.get("status", "")})
            level = logging.INFO if status == 200 else logging.WARNING
            log.log(level, "file %s %s (HTTP %s)%s", info.get("file_id"), info.get("status"),
                    status, ": " + "; ".join(info["errors"]) if info.get("errors") else "",
                    extra={"attrs": {"ach.file.id": info.get("file_id"),
                                     "ach.file.status": info.get("status"),
                                     "http.response.status_code": status,
                                     "ach.bank.name": self.bank_name}})
            return status, resp

    def status(self, file_id):
        return self._req("GET", f"/v1/files/{file_id}")

    # -- inbound ------------------------------------------------------------
    def inbox(self):
        return self._req("GET", "/v1/mailbox")

    def download(self, name):
        return self._req("GET", f"/v1/mailbox/{name}")

    def ack(self, name):
        return self._req("POST", f"/v1/mailbox/{name}/ack")

    def receive(self, ledger: dict, save_dir: Path | None = None, verbose=True):
        """Pull every mailbox file, post/return entries, transmit returns. Returns a report."""
        with telemetry.span("ach.client.receive", **{
                "ach.bank.routing": self.routing, "ach.bank.name": self.bank_name}) as s:
            report = self._receive(ledger, save_dir, verbose)
            s.set_attributes({"ach.mailbox.files": len(report["files"]),
                              "ach.returns_sent": bool(report["returns_sent"])})
            return report

    def _post(self, outcome: str, code: str | None):
        telemetry.LEDGER_POSTINGS.add(1, {"ach.bank.name": self.bank_name,
                                          "ach.outcome": outcome, "ach.code": code or ""})

    def _receive(self, ledger: dict, save_dir: Path | None, verbose: bool):
        report = {"files": [], "returns_sent": None}
        _, box = self.inbox()
        items = []
        for meta in box["files"]:
            name = meta["name"]
            with telemetry.step("process_mailbox_file", span_name="ach.client.process_file",
                                logger=log, **{"ach.mailbox.file": name,
                                               "ach.bank.name": self.bank_name}) as fs:
                entry_log = self._process_file(name, ledger, save_dir, verbose, items, fs.span)
                fs.detail.update(mailbox_file=name, entries=len(entry_log or []))
                if entry_log is None:
                    fs.reject("invalid_mailbox_file")
            report["files"].append({"name": name, "valid": entry_log is not None,
                                    "entries": entry_log or []})

        if items:
            created = datetime.fromisoformat(self.sim_time).astimezone(ET) if self.sim_time \
                else datetime.now(ET)
            rf = rdfi.build_response_file(
                items, rdfi_routing=self.routing, origin=self.routing,
                origin_name=PARTICIPANTS[self.routing]["name"],
                destination=OPERATOR_ROUTING, destination_name="SIM ACH OPERATOR",
                created=created.replace(tzinfo=None), file_id_modifier="R")
            text = rf.to_text()
            if save_dir:
                (save_dir / f"outgoing_returns_{created:%Y%m%dT%H%M}.ach").write_text(text)
            if verbose:
                print("\n--- returns/NOCs we are sending back ---")
                print(nacha.describe(text))
            log.info("sending %d return/NOC entries back through the operator", len(items),
                     extra={"attrs": {"ach.bank.name": self.bank_name,
                                      "ach.entries": len(items)}})
            report["returns_sent"] = self.send_file(text)
        return report

    def _process_file(self, name, ledger, save_dir, verbose, items, span):
        """Download, post and acknowledge one mailbox file. Appends returns/NOCs to
        `items`; returns the per-entry log, or None if the file failed validation."""
        _, text = self.download(name)
        res = nacha.validate(text)
        if save_dir:
            save_dir.mkdir(parents=True, exist_ok=True)
            (save_dir / name).write_text(text)
        if verbose:
            print(f"\n--- {name} ({'valid' if res.ok else 'INVALID'}) ---")
            print(nacha.describe(text))
        entry_log = None
        if res.ok:
            entry_log = []
            f = nacha.parse(text)
            for b in f.batches:
                for e in b.entries:
                    if b.sec_code == "COR" or e.transaction_code in ("21", "26", "31", "36"):
                        a = e.addenda[0]
                        code = a.get("return_reason_code") or a.get("change_code")
                        label = "NOC" if b.sec_code == "COR" else "return"
                        self._post(f"{label.lower()}_received", code)
                        entry_log.append({"trace": e.trace_number, "outcome":
                                          f"{label} {code} on our trace "
                                          f"{a['original_entry_trace_number']}"})
                        log.info("%s %s received for our trace %s", label, code,
                                 a["original_entry_trace_number"],
                                 extra={"attrs": {"ach.code": code, "ach.mailbox.file": name,
                                                  "ach.trace": a["original_entry_trace_number"]}})
                        continue
                    for kind, code, data in rdfi.decide(e, b, ledger):
                        self._post(kind, code)
                        outcome = rdfi.describe_outcome(kind, code, data)
                        entry_log.append({"trace": e.trace_number, "outcome": outcome})
                        log.info("entry %s %s", e.trace_number, outcome,
                                 extra={"attrs": {"ach.trace": e.trace_number,
                                                  "ach.outcome": kind, "ach.code": code,
                                                  "ach.amount_cents": e.amount,
                                                  "ach.mailbox.file": name}})
                        if kind != "post":
                            items.append((b, e, kind, code, data))
            span.set_attribute("ach.entries", len(entry_log))
        else:
            telemetry.mark_error(span, "mailbox file failed validation")
            log.error("mailbox file %s failed validation: %s", name, "; ".join(res.errors),
                      extra={"attrs": {"ach.mailbox.file": name}})
        self.ack(name)
        return entry_log


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--key", default=DEFAULT_KEY, help="bank API key (default: First Example Bank)")
    ap.add_argument("--sim-time", help="simulated submission time, ISO 8601 (default: now)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send", help="validate and transmit a Nacha file")
    s.add_argument("file")
    s.add_argument("--no-precheck", action="store_true",
                   help="skip local validation (to see the operator reject a bad file)")
    sub.add_parser("inbox", help="list files waiting in this bank's mailbox")
    r = sub.add_parser("receive", help="pull files, post to ledger, send returns")
    r.add_argument("--ledger", required=True, help="JSON file of this bank's accounts")
    r.add_argument("--save-dir", default="received_files")
    st = sub.add_parser("status", help="status of a submitted file")
    st.add_argument("file_id")
    v = sub.add_parser("validate", help="validate and describe a file locally (no network)")
    v.add_argument("file")
    args = ap.parse_args(argv)
    telemetry.configure_logging(os.environ.get("ACH_SIM_LOG_LEVEL", "WARNING"))
    telemetry.setup("ach-bank-client")

    if args.cmd == "validate":
        text = Path(args.file).read_text()
        res = nacha.validate(text)
        try:
            print(nacha.describe(text))
        except nacha.NachaParseError:
            pass
        print("VALID" if res.ok else "INVALID")
        for e in res.errors:
            print("  error:", e)
        for w in res.warnings:
            print("  warning:", w)
        return 0 if res.ok else 1

    c = OperatorClient(args.url, args.key, args.sim_time)
    if args.cmd == "send":
        status, resp = c.send_file(Path(args.file).read_text(), not args.no_precheck)
        print(f"HTTP {status}" if status else "NOT SENT")
        print(json.dumps(resp, indent=2))
        return 0 if status == 200 else 1
    if args.cmd == "inbox":
        print(json.dumps(c.inbox()[1], indent=2))
    elif args.cmd == "status":
        print(json.dumps(c.status(args.file_id)[1], indent=2))
    elif args.cmd == "receive":
        path = Path(args.ledger)
        ledger = json.loads(path.read_text())
        report = c.receive(ledger, Path(args.save_dir))
        path.write_text(json.dumps(ledger, indent=2))
        print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
