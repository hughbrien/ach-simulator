#!/usr/bin/env python3
"""RDFI service: one receiving bank the simulator plays (mode "auto" in participants.py).

Owns that bank's receiver accounts. For each forwarded file it posts, returns or
NOCs every entry (rdfi.decide), then transmits its returns and NOCs back through
the operator as a file of its own, authenticated with the bank's API key, the way
a real RDFI does.

    POST /v1/inbound     body = forwarded Nacha file; headers X-Sim-Time, X-File-Id
                         -> {"bank", "decisions": [...], "returns_file": {...} | null}
    GET  /v1/accounts    current balances
    GET  /v1/health

    python3 -m ach_sim.rdfi_service --routing 111000025 --port 8773 \\
        --operator-url http://127.0.0.1:8770
"""
from __future__ import annotations

import argparse
import copy
import logging
import threading
from datetime import datetime
from http.server import ThreadingHTTPServer

from . import nacha, rdfi, telemetry
from .participants import OPERATOR_NAME, OPERATOR_ROUTING, PARTICIPANTS, RDFI_ACCOUNTS
from .service import DEFAULT_PORTS, HOST, TracedHandler, call, default_url, run_service, slug
from .settlement import ET

MODIFIERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
RETURN_TX = ("21", "26", "31", "36")
log = logging.getLogger("ach_sim.rdfi")


def service_name(routing: str) -> str:
    return f"ach-rdfi-{slug(PARTICIPANTS[routing]['name'])}"


class RdfiBank:
    def __init__(self, routing: str, operator_url: str | None = None):
        if routing not in RDFI_ACCOUNTS:
            raise ValueError(f"{routing} is not a simulated RDFI")
        self.routing = routing
        self.name = PARTICIPANTS[routing]["name"]
        self.api_key = PARTICIPANTS[routing]["api_key"]
        self.operator_url = operator_url or default_url("operator")
        self.accounts = copy.deepcopy(RDFI_ACCOUNTS[routing])
        self.lock = threading.Lock()
        self.files_sent = 0

    def receive(self, text: str, sim_time: str | None, file_id: str = "") -> dict:
        with telemetry.span("ach.rdfi.process", **{
                "ach.rdfi.routing": self.routing, "ach.rdfi.name": self.name,
                "ach.file.id": file_id}) as s:
            f = nacha.parse(text)
            decisions, items = [], []
            with self.lock:
                for b in f.batches:
                    for e in b.entries:
                        if b.sec_code == "COR" or e.transaction_code in RETURN_TX:
                            a = e.addenda[0] if e.addenda else {}
                            code = a.get("return_reason_code") or a.get("change_code")
                            decisions.append({"trace": e.trace_number, "kind": "received",
                                              "code": code})
                            log.info("%s %s received for trace %s", "NOC" if b.sec_code == "COR"
                                     else "return", code, a.get("original_entry_trace_number"),
                                     extra={"attrs": {"ach.code": code,
                                                      "ach.trace": e.trace_number}})
                            continue
                        for kind, code, data in rdfi.decide(e, b, self.accounts):
                            outcome = rdfi.describe_outcome(kind, code, data)
                            decisions.append({"trace": e.trace_number, "kind": kind,
                                              "code": code, "outcome": outcome,
                                              "amount_cents": e.amount})
                            s.add_event("ach.rdfi.decision", {"ach.trace": e.trace_number,
                                                              "ach.outcome": outcome})
                            telemetry.ENTRY_OUTCOMES.add(1, {
                                "ach.rdfi.name": self.name, "ach.outcome": kind,
                                "ach.code": code or "", "ach.sec": b.sec_code})
                            log.info("entry %s %s", e.trace_number, outcome,
                                     extra={"attrs": {"ach.trace": e.trace_number,
                                                      "ach.outcome": kind, "ach.code": code,
                                                      "ach.amount_cents": e.amount,
                                                      "ach.file.id": file_id}})
                            if kind != "post":
                                items.append((b, e, kind, code, data))
                self.files_sent += bool(items)
                modifier = MODIFIERS[(self.files_sent - 1) % len(MODIFIERS)]
            s.set_attributes({"ach.entries": len(decisions), "ach.responses": len(items)})
            returns_file = self._send_returns(items, sim_time, modifier) if items else None
            return {"bank": self.routing, "decisions": decisions, "returns_file": returns_file}

    def _send_returns(self, items, sim_time: str | None, modifier: str) -> dict:
        """Transmit returns and NOCs to the operator as this bank's own file."""
        created = (datetime.fromisoformat(sim_time) if sim_time else datetime.now(ET))
        created = (created if created.tzinfo else created.replace(tzinfo=ET)).astimezone(ET)
        rf = rdfi.build_response_file(
            items, rdfi_routing=self.routing, origin=self.routing, origin_name=self.name,
            destination=OPERATOR_ROUTING, destination_name=OPERATOR_NAME,
            created=created.replace(tzinfo=None), file_id_modifier=modifier)
        headers = {"X-API-Key": self.api_key}
        if sim_time:
            headers["X-Sim-Time"] = sim_time
        with telemetry.span("ach.rdfi.send_returns", **{
                "ach.rdfi.name": self.name, "ach.entries": len(items)}) as s:
            status, ack = call("POST", f"{self.operator_url}/v1/files", body=rf.to_text(),
                               headers=headers)
            ack = ack if isinstance(ack, dict) else {}
            s.set_attributes({"ach.file.id": ack.get("file_id", ""),
                              "ach.file.status": ack.get("status", "")})
        level = logging.INFO if status == 200 else logging.ERROR
        log.log(level, "sent %d returns/NOCs to the operator: HTTP %s %s", len(items), status,
                ack.get("file_id", ""), extra={"attrs": {"ach.file.id": ack.get("file_id"),
                                                         "http.response.status_code": status}})
        return {"status": status, "file_id": ack.get("file_id"), "errors": ack.get("errors")}


def make_handler(bank: RdfiBank):
    class Handler(TracedHandler):
        def handle_get(self):
            if self.route_path == "/v1/health":
                return self.send(200, {"status": "ok", "service": service_name(bank.routing)})
            if self.route_path == "/v1/accounts":
                with bank.lock:
                    return self.send(200, {"bank": bank.routing, "accounts": bank.accounts})
            super().handle_get()

        def handle_post(self):
            if self.route_path != "/v1/inbound":
                return super().handle_post()
            try:
                result = bank.receive(self.body_text(), self.headers.get("X-Sim-Time"),
                                      self.headers.get("X-File-Id", ""))
            except nacha.NachaParseError as exc:
                return self.send(422, {"error": str(exc)})
            self.send(200, result)

    return Handler


def serve(routing: str, host=HOST, port: int | None = None, operator_url: str | None = None):
    bank = RdfiBank(routing, operator_url)
    port = DEFAULT_PORTS[routing] if port is None else port
    return bank, ThreadingHTTPServer((host, port), make_handler(bank))


def main():
    auto = [rt for rt, p in PARTICIPANTS.items() if p["mode"] == "auto"]
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--routing", required=True, choices=auto)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, help="default: 8773 + the bank's position")
    ap.add_argument("--operator-url", default=default_url("operator"))
    args = ap.parse_args()
    bank, httpd = serve(args.routing, args.host, args.port, args.operator_url)
    name = service_name(args.routing)
    run_service(httpd, name,
                f"{name} ({bank.name} {bank.routing}) listening on "
                f"http://{args.host}:{httpd.server_address[1]}",
                resource_attrs={"ach.bank.routing": bank.routing, "ach.bank.name": bank.name})


if __name__ == "__main__":
    main()
