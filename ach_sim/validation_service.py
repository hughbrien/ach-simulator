#!/usr/bin/env python3
"""ACH validation service: the operator's Nacha format and balancing edits.

    POST /v1/validate     body = Nacha file text  ->  {"ok", "errors", "warnings"}
    GET  /v1/health

    python3 -m ach_sim.validation_service --port 8771
"""
from __future__ import annotations

import argparse
import logging
from http.server import ThreadingHTTPServer

from . import nacha, telemetry
from .service import DEFAULT_PORTS, HOST, TracedHandler, run_service

SERVICE_NAME = "ach-validation"
log = logging.getLogger("ach_sim.validation")


class Handler(TracedHandler):
    def handle_get(self):
        if self.route_path == "/v1/health":
            return self.send(200, {"status": "ok", "service": SERVICE_NAME})
        super().handle_get()

    def handle_post(self):
        if self.route_path != "/v1/validate":
            return super().handle_post()
        text = self.body_text()
        with telemetry.span("ach.validate", **{"ach.file.bytes": len(text)}) as s:
            res = nacha.validate(text)
            s.set_attributes({"ach.validation.errors": len(res.errors),
                              "ach.validation.warnings": len(res.warnings),
                              "ach.file.valid": res.ok})
        telemetry.VALIDATIONS.add(1, {"ach.result": "valid" if res.ok else "invalid"})
        if not res.ok:
            log.warning("file failed edits: %s", "; ".join(res.errors),
                        extra={"attrs": {"ach.validation.errors": res.errors}})
        self.send(200, {"ok": res.ok, "errors": res.errors, "warnings": res.warnings})


def serve(host=HOST, port=DEFAULT_PORTS["validation"]):
    return ThreadingHTTPServer((host, port), Handler)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORTS["validation"])
    args = ap.parse_args()
    httpd = serve(args.host, args.port)
    run_service(httpd, SERVICE_NAME,
                f"{SERVICE_NAME} listening on http://{args.host}:{httpd.server_address[1]}")


if __name__ == "__main__":
    main()
