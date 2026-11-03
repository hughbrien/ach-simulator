#!/usr/bin/env python3
"""ACH settlement service: settlement dates and Same Day windows.

    POST /v1/settle   {"submitted_at": ISO 8601,
                       "batches": [{"batch": 1, "effective_date": "YYMMDD",
                                    "same_day_eligible": true}, ...]}
                  ->  {"batches": [{"batch": 1, "settlement_date": "YYYY-MM-DD",
                                    "window": "SDA-1"}, ...]}
    GET  /v1/health

    python3 -m ach_sim.settlement_service --port 8772
"""
from __future__ import annotations

import argparse
from datetime import datetime
from http.server import ThreadingHTTPServer

import logging

from . import nacha, telemetry
from .service import DEFAULT_PORTS, HOST, TracedHandler, run_service
from .settlement import ET, settlement_for

SERVICE_NAME = "ach-settlement"
log = logging.getLogger("ach_sim.settlement")


class Handler(TracedHandler):
    def handle_get(self):
        if self.route_path == "/v1/health":
            return self.send(200, {"status": "ok", "service": SERVICE_NAME})
        super().handle_get()

    def handle_post(self):
        if self.route_path != "/v1/settle":
            return super().handle_post()
        req = self.body_json() or {}
        try:
            now = datetime.fromisoformat(req["submitted_at"])
            now = now if now.tzinfo else now.replace(tzinfo=ET)
            batches = req["batches"]
        except (KeyError, TypeError, ValueError) as exc:
            return self.send(400, {"error": f"bad request: {exc}"})
        out = []
        with telemetry.step("settlement_dates", span_name="ach.settle", logger=log,
                            **{"ach.batches": len(batches), "ach.sim_time": now.isoformat()}) as st:
            for b in batches:
                sdate, window = settlement_for(nacha.parse_yymmdd(b["effective_date"]), now,
                                               b.get("same_day_eligible", True))
                out.append({"batch": b.get("batch"), "settlement_date": sdate.isoformat(),
                            "window": window})
                telemetry.SETTLEMENTS.add(1, {"ach.window": window})
            st.span.set_attribute("ach.windows", sorted({b["window"] for b in out}))
            st.detail.update(batches=len(out), windows=sorted({b["window"] for b in out}))
        self.send(200, {"batches": out})


def serve(host=HOST, port=DEFAULT_PORTS["settlement"]):
    return ThreadingHTTPServer((host, port), Handler)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORTS["settlement"])
    args = ap.parse_args()
    httpd = serve(args.host, args.port)
    run_service(httpd, SERVICE_NAME,
                f"{SERVICE_NAME} listening on http://{args.host}:{httpd.server_address[1]}")


if __name__ == "__main__":
    main()
