#!/usr/bin/env python3
"""Read-only web dashboard for observing the simulator.

Reads the operator's data directory (events.jsonl, received/, mailbox/) and serves
a page that refreshes itself, so it works alongside a running operator or after
run_demo.py has finished.

    python3 -m ach_sim.dashboard --data-dir sim_data --port 8780

    GET /                          the dashboard page
    GET /api/state                 summary, files, events, mailboxes
    GET /api/file/<file_id>        one submitted file with per-entry outcomes
    GET /api/mailbox/<rt>/<name>   one mailbox file (add ?picked_up=1 for picked-up files)
"""
from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import nacha
from .participants import OPERATOR_NAME, OPERATOR_ROUTING, PARTICIPANTS

PAGE = Path(__file__).with_name("dashboard.html")
SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")
RETURN_TX = {"21", "26", "31", "36"}
BANKS = {OPERATOR_ROUTING: OPERATOR_NAME, **{rt: p["name"] for rt, p in PARTICIPANTS.items()}}


def bank(rt: str) -> str:
    return BANKS.get(rt, rt)


def outcome_kind(text: str) -> str:
    for prefix, kind in (("posted", "post"), ("return", "return"), ("NOC", "noc")):
        if text.startswith(prefix):
            return kind
    return "info"


class Observer:
    def __init__(self, data_dir: Path):
        self.data = Path(data_dir)
        self._parsed: dict = {}  # file_id -> (mtime, AchFile)

    def events(self) -> list:
        p = self.data / "events.jsonl"
        if not p.exists():
            return []
        out = []
        for ln in p.read_text().splitlines():
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:  # a line being written right now
                pass
        return out

    def parsed(self, file_id: str):
        p = self.data / "received" / f"{file_id}.ach"
        if not p.exists():
            return None
        mtime = p.stat().st_mtime
        hit = self._parsed.get(file_id)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            f = nacha.parse(p.read_text())
        except Exception:
            f = None
        self._parsed[file_id] = (mtime, f)
        return f

    def outcomes(self, events: list) -> dict:
        """(file_id, trace) -> list of {"kind", "text"} for every forward entry seen."""
        out: dict = {}

        def add(fid, trace, kind, text):
            out.setdefault((fid, trace), []).append({"kind": kind, "text": text})

        last_file_for_trace: dict = {}
        for ev in events:
            kind, fid = ev["event"], ev.get("file_id")
            if kind == "rdfi_decision":
                add(fid, ev["trace"], outcome_kind(ev["outcome"]),
                    f"{ev['outcome']} ({bank(ev['rdfi'])})")
            elif kind == "operator_return":
                add(fid, ev["trace"], "return", f"return {ev['code']} by the operator")
            elif kind == "return_or_noc_received":
                add(fid, ev["trace"], "info", f"{ev['code']} received by {bank(ev['bank'])}")
            elif kind == "entries_forwarded":
                mode = PARTICIPANTS.get(ev["rdfi"], {}).get("mode")
                for t in ev["traces"]:
                    last_file_for_trace[t] = fid
                    if mode == "mailbox":
                        add(fid, t, "info", f"delivered to {bank(ev['rdfi'])} mailbox")
            elif kind == "file_accepted":
                # A mailbox bank answers with its own return/NOC file; credit each item
                # to the most recent file that carried the original trace.
                f = self.parsed(fid)
                for b in (f.batches if f else []):
                    for e in b.entries:
                        if e.transaction_code not in RETURN_TX or not e.addenda:
                            continue
                        a = e.addenda[0]
                        code = a.get("return_reason_code") or a.get("change_code")
                        orig = a.get("original_entry_trace_number")
                        target = last_file_for_trace.get(orig)
                        sender = ev["submitter"]
                        if not target or PARTICIPANTS.get(sender, {}).get("mode") != "mailbox":
                            continue
                        if code.startswith("R"):
                            kind_, text = "return", f"return {code} {nacha.RETURN_REASONS.get(code, '')}"
                        else:
                            kind_, text = "noc", f"NOC {code} {nacha.CHANGE_CODES.get(code, '')}"
                        add(target, orig, kind_, f"{text} ({bank(sender)}, file {fid})")
        return out

    def state(self) -> dict:
        events = self.events()
        outcomes = self.outcomes(events)
        files, forwarded = [], 0
        for ev in events:
            if ev["event"] == "file_accepted":
                files.append({
                    "file_id": ev["file_id"], "status": "accepted",
                    "submitter": bank(ev["submitter"]), "sim_time": ev.get("sim_time"),
                    "batches": ev["batches_detail"], "entries": ev["entries"],
                    "debit_cents": ev["total_debit_cents"],
                    "credit_cents": ev["total_credit_cents"], "warnings": ev["warnings"]})
            elif ev["event"] == "file_rejected":
                files.append({"file_id": ev["file_id"], "status": "rejected",
                              "submitter": bank(ev["submitter"]),
                              "sim_time": ev.get("sim_time"), "errors": ev["errors"]})
            elif ev["event"] == "entries_forwarded":
                forwarded += len(ev["traces"])
        kinds = [o["kind"] for os_ in outcomes.values() for o in os_]
        accepted = [f for f in files if f["status"] == "accepted"]
        stats = {
            "accepted": len(accepted),
            "rejected": len(files) - len(accepted),
            "forwarded": forwarded,
            "posted": kinds.count("post"),
            "returns": kinds.count("return"),
            "nocs": kinds.count("noc"),
            "credit_cents": sum(f["credit_cents"] for f in accepted),
            "debit_cents": sum(f["debit_cents"] for f in accepted),
        }
        for f in files:
            ks = [o["kind"] for (fid, _), os_ in outcomes.items() if fid == f["file_id"]
                  for o in os_]
            f["returns"], f["nocs"] = ks.count("return"), ks.count("noc")
        return {"data_dir": str(self.data), "stats": stats, "files": files[::-1],
                "events": events[-400:][::-1], "mailboxes": self.mailboxes()}

    def mailboxes(self) -> list:
        out = []
        for rt, p in PARTICIPANTS.items():
            box = self.data / "mailbox" / rt
            out.append({"routing": rt, "name": p["name"], "mode": p["mode"],
                        "waiting": sorted(x.name for x in box.glob("*.ach")),
                        "picked_up": sorted(x.name for x in (box / "picked_up").glob("*.ach"))})
        return out

    def file_detail(self, file_id: str):
        f = self.parsed(file_id)
        p = self.data / "received" / f"{file_id}.ach"
        if not p.exists():
            return None
        events = self.events()
        outcomes = self.outcomes(events)
        status = next((e for e in events if e.get("file_id") == file_id
                       and e["event"] in ("file_accepted", "file_rejected")), {})
        batches = []
        for b in (f.batches if f else []):
            entries = []
            for e in b.entries:
                a = e.addenda[0] if e.addenda else {}
                entries.append({
                    "trace": e.trace_number, "tx": e.transaction_code,
                    "tx_desc": nacha.TRANSACTION_CODES.get(e.transaction_code, ""),
                    "rdfi": bank(e.rdfi_routing), "account": e.account.strip(),
                    "name": e.name.strip(), "amount_cents": e.amount,
                    "credit": e.is_credit,
                    "addenda": (a.get("payment_related_information")
                                or a.get("return_reason_code") or a.get("change_code") or "")
                    .strip(),
                    "outcomes": outcomes.get((file_id, e.trace_number), [])})
            batches.append({"batch": b.batch_number, "sec": b.sec_code,
                            "company": b.company_name.strip(),
                            "description": b.entry_description.strip(),
                            "effective_date": b.effective_date, "entries": entries})
        text = p.read_text()
        try:
            described = nacha.describe(text)
        except Exception as exc:
            described = f"(could not describe file: {exc})"
        return {"file_id": file_id, "event": status, "batches": batches,
                "describe": described, "raw": text}

    def mailbox_file(self, rt: str, name: str, picked_up: bool):
        if rt not in PARTICIPANTS or not SAFE.match(name):
            return None
        p = self.data / "mailbox" / rt / ("picked_up" if picked_up else "") / name
        if not p.exists():
            return None
        text = p.read_text()
        try:
            described = nacha.describe(text)
        except Exception as exc:
            described = f"(could not describe file: {exc})"
        return {"name": name, "describe": described, "raw": text}


def make_handler(obs: Observer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SimACHDashboard/1.0"

        def log_message(self, fmt, *args):  # polling would flood the console
            pass

        def _send(self, code, body, content_type="application/json"):
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            parts = [x for x in url.path.split("/") if x]
            if not parts:
                return self._send(200, PAGE.read_text(), "text/html; charset=utf-8")
            if parts == ["api", "state"]:
                return self._send(200, obs.state())
            if len(parts) == 3 and parts[:2] == ["api", "file"] and SAFE.match(parts[2]):
                d = obs.file_detail(parts[2])
                return self._send(200, d) if d else self._send(404, {"error": "no such file"})
            if len(parts) == 4 and parts[:2] == ["api", "mailbox"]:
                picked = parse_qs(url.query).get("picked_up") == ["1"]
                d = obs.mailbox_file(parts[2], parts[3], picked)
                return self._send(200, d) if d else self._send(404, {"error": "no such file"})
            self._send(404, {"error": "not found"})

    return Handler


def serve(host="127.0.0.1", port=8780, data_dir="sim_data"):
    return ThreadingHTTPServer((host, port), make_handler(Observer(Path(data_dir))))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--data-dir", default="sim_data")
    args = ap.parse_args()
    httpd = serve(args.host, args.port, args.data_dir)
    print(f"ACH dashboard on http://{args.host}:{args.port}  (watching {args.data_dir}/)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
