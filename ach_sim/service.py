"""HTTP plumbing shared by every simulator service and the bank client.

    call(...)          an HTTP request inside a client span that propagates the trace
    TracedHandler      request handler base: one server span per request, JSON helpers
    run_service(...)   the run loop of a service process (logging, telemetry, signals)

Default ports, so the services find each other when started by hand:
operator 8770, validation 8771, settlement 8772, RDFI services 8773+, dashboard 8780.
"""
from __future__ import annotations

import json
import logging
import signal
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

from . import telemetry
from .participants import PARTICIPANTS
from .settlement import ET

log = logging.getLogger("ach_sim.http")

HOST = "127.0.0.1"
DEFAULT_PORTS = {"operator": 8770, "validation": 8771, "settlement": 8772, "dashboard": 8780}
# One RDFI service per bank the simulator plays (mode "auto"), from 8773 up.
for _i, _rt in enumerate(rt for rt, p in PARTICIPANTS.items() if p["mode"] == "auto"):
    DEFAULT_PORTS[_rt] = 8773 + _i


def default_url(name: str) -> str:
    return f"http://{HOST}:{DEFAULT_PORTS[name]}"


def free_port(host: str = HOST) -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def slug(name: str) -> str:
    return "-".join(name.lower().split())


# ---------------------------------------------------------------------------
# Client side
# ---------------------------------------------------------------------------
def call(method: str, url: str, *, body: str | None = None, json_body=None,
         headers: dict | None = None, timeout: float = 30):
    """Send one request inside a client span, with W3C trace context in the headers.
    Returns (status, parsed JSON or text). Network failures raise after being
    recorded on the span."""
    headers = dict(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif body is not None:
        data = body.encode("ascii")
        headers.setdefault("Content-Type", "text/plain")
    parts = urllib.parse.urlsplit(url)
    route = telemetry.http_route(parts.path)
    metric_attrs = {"http.request.method": method, "http.route": route,
                    "server.port": parts.port or 80}
    start = time.perf_counter()
    with telemetry.span(f"{method} {route}", "client", **metric_attrs,
                        **{"url.full": url, "server.address": parts.hostname}) as s:
        telemetry.inject(headers)
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status, raw, ctype = (resp.status, resp.read().decode(),
                                      resp.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as e:
            with e:
                status, raw, ctype = e.code, e.read().decode(), e.headers.get("Content-Type", "")
        except (urllib.error.URLError, OSError) as e:
            telemetry.mark_error(s, e)
            telemetry.HTTP_CLIENT_DURATION.record(
                time.perf_counter() - start, {**metric_attrs, "error.type": type(e).__name__})
            log.error("%s %s failed: %s", method, url, e,
                      extra={"attrs": {**metric_attrs, "error.type": type(e).__name__}})
            raise
        s.set_attribute("http.response.status_code", status)
        if status >= 400:
            s.set_attribute("error.type", str(status))
            telemetry.mark_error(s, f"HTTP {status}")
    telemetry.HTTP_CLIENT_DURATION.record(time.perf_counter() - start,
                                          {**metric_attrs, "http.response.status_code": status})
    return status, (json.loads(raw) if "json" in ctype else raw)


# ---------------------------------------------------------------------------
# Server side
# ---------------------------------------------------------------------------
class TracedHandler(BaseHTTPRequestHandler):
    """Subclasses implement handle_get / handle_post and answer with send()."""
    server_version = "SimACH/1.0"

    def log_message(self, fmt, *args):  # requests are logged by _traced instead
        pass

    def do_GET(self):
        self._traced(self.handle_get)

    def do_POST(self):
        self._traced(self.handle_post)

    def handle_get(self):
        self.send(404, {"error": "not found"})

    def handle_post(self):
        self.send(404, {"error": "not found"})

    @property
    def route_path(self) -> str:
        return self.path.split("?")[0].rstrip("/") or "/"

    def _traced(self, handler):
        """Run one request inside a server span that continues the caller's trace."""
        path = self.path.split("?")[0]
        route = telemetry.http_route(path)
        attrs = {"http.request.method": self.command, "http.route": route}
        self._status = 500
        start = time.perf_counter()
        with telemetry.span(f"{self.command} {route}", "server", parent_headers=self.headers,
                            **attrs, **{"url.path": path,
                                        "client.address": self.client_address[0],
                                        "server.port": self.server.server_address[1]}) as s:
            try:
                handler()
            except Exception as exc:
                telemetry.mark_error(s, exc)
                log.exception("%s %s failed", self.command, path,
                              extra={"attrs": {**attrs, "url.path": path}})
                self.send(500, {"error": "internal error"})
            s.set_attribute("http.response.status_code", self._status)
            if self._status >= 500:
                telemetry.mark_error(s, f"HTTP {self._status}")
            log.info("%s %s %s", self.command, path, self._status,
                     extra={"attrs": {**attrs, "url.path": path,
                                      "http.response.status_code": self._status}})
        telemetry.HTTP_SERVER_DURATION.record(
            time.perf_counter() - start, {**attrs, "http.response.status_code": self._status})

    def send(self, code: int, body, content_type="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body, indent=2).encode()
        self._status = code
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body_text(self) -> str:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length).decode("ascii", errors="replace")

    def body_json(self):
        return json.loads(self.body_text() or "null")

    def sim_time(self) -> datetime:
        """The simulated clock from X-Sim-Time (Eastern Time if no offset), else now."""
        raw = self.headers.get("X-Sim-Time")
        if raw:
            t = datetime.fromisoformat(raw)
            return t if t.tzinfo else t.replace(tzinfo=ET)
        return datetime.now(timezone.utc)


def run_service(httpd, service_name: str, banner: str, resource_attrs: dict | None = None):
    """Serve until Ctrl+C or SIGTERM, exporting buffered telemetry on the way out."""
    telemetry.configure_logging(service=service_name)
    telemetry.setup(service_name, resource_attrs=resource_attrs)
    print(banner, flush=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        telemetry.flush()
