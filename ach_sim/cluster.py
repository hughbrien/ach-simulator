#!/usr/bin/env python3
"""Start the distributed simulator: every service in its own process.

    python3 -m ach_sim.cluster                          # default ports, plus the dashboard
    python3 -m ach_sim.cluster --data-dir sim_data --no-dashboard --log-dir sim_data/logs

    ach-operator                        8770   gateway banks talk to
    ach-validation                      8771   Nacha edits
    ach-settlement                      8772   settlement dates and Same Day windows
    ach-rdfi-second-sample-bank         8773   simulated receiving bank
    ach-rdfi-third-demo-credit-union    8774   simulated receiving bank
    dashboard                           8780   web UI (read-only, not instrumented)

Each process exports telemetry under its own service name (see telemetry.py), so
an observability backend sees a distributed system. Ctrl+C stops everything.

start_processes() does the same from Python (run_demo.py uses it); start_local()
runs the same services in threads of the current process (tests).
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import operator_server, rdfi_service, settlement_service, validation_service
from .participants import PARTICIPANTS
from .service import DEFAULT_PORTS, HOST, free_port

ROOT = Path(__file__).resolve().parents[1]
AUTO_BANKS = [rt for rt, p in PARTICIPANTS.items() if p["mode"] == "auto"]


def _url(port: int) -> str:
    return f"http://{HOST}:{port}"


def plan(ports: dict, data_dir: Path, dashboard: bool = True) -> list[dict]:
    """The processes to start: name, module, arguments and health URL."""
    op = _url(ports["operator"])
    specs = [
        {"name": "ach-validation", "module": "ach_sim.validation_service",
         "args": ["--port", str(ports["validation"])], "port": ports["validation"]},
        {"name": "ach-settlement", "module": "ach_sim.settlement_service",
         "args": ["--port", str(ports["settlement"])], "port": ports["settlement"]},
    ]
    for rt in AUTO_BANKS:
        specs.append({"name": rdfi_service.service_name(rt), "module": "ach_sim.rdfi_service",
                      "args": ["--routing", rt, "--port", str(ports[rt]), "--operator-url", op],
                      "port": ports[rt]})
    specs.append({"name": "ach-operator", "module": "ach_sim.operator_server",
                  "args": ["--port", str(ports["operator"]), "--data-dir", str(data_dir),
                           "--validation-url", _url(ports["validation"]),
                           "--settlement-url", _url(ports["settlement"]),
                           *[a for rt in AUTO_BANKS for a in ("--rdfi", f"{rt}={_url(ports[rt])}")]],
                  "port": ports["operator"]})
    for s in specs:
        s["health"] = _url(s["port"]) + "/v1/health"
    if dashboard:
        specs.append({"name": "dashboard", "module": "ach_sim.dashboard",
                      "args": ["--port", str(ports["dashboard"]), "--data-dir", str(data_dir)],
                      "port": ports["dashboard"], "health": _url(ports["dashboard"]) + "/api/state"})
    return specs


def _port_in_use(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex((HOST, port)) == 0


def _healthy(url: str) -> bool:
    try:  # plain urllib on purpose: health polling should not produce spans
        with urllib.request.urlopen(url, timeout=1) as r:
            return r.status == 200
    except OSError:
        return False


class ProcessCluster:
    def __init__(self, specs, procs, log_dir):
        self.specs, self.procs, self.log_dir = specs, procs, log_dir
        self.operator_url = next(_url(s["port"]) for s in specs if s["name"] == "ach-operator")

    def alive(self) -> bool:
        return all(p.poll() is None for p in self.procs)

    def stop(self, timeout: float = 10):
        """SIGTERM every service (they export buffered telemetry on the way out)."""
        for p in reversed(self.procs):
            if p.poll() is None:
                p.terminate()
        deadline = time.time() + timeout
        for p in self.procs:
            try:
                p.wait(max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                p.kill()


def start_processes(data_dir, *, ports: dict | None = None, free_ports: bool = False,
                    dashboard: bool = True, log_dir=None, timeout: float = 20) -> ProcessCluster:
    """Start each service as a subprocess and wait until all answer their health check.
    With free_ports=True every service gets an unused port (for scripts and demos)."""
    if free_ports:
        ports = {k: free_port() for k in DEFAULT_PORTS}
    ports = {**DEFAULT_PORTS, **(ports or {})}
    specs = plan(ports, Path(data_dir), dashboard)
    busy = [f"{s['name']} :{s['port']}" for s in specs if _port_in_use(s["port"])]
    if busy:
        raise RuntimeError("ports already in use (is another simulator running?): "
                           + ", ".join(busy))
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    env.pop("OTEL_SERVICE_NAME", None)  # each service names itself
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
    procs = []
    for s in specs:
        out = open(Path(log_dir) / f"{s['name']}.log", "w") if log_dir else None
        procs.append(subprocess.Popen([sys.executable, "-m", s["module"], *s["args"]],
                                      cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT
                                      if out else None))
        if out:
            out.close()  # the child holds its own copy of the descriptor
    cluster = ProcessCluster(specs, procs, log_dir)
    deadline = time.time() + timeout
    pending = list(zip(specs, procs))
    while pending:
        for s, p in list(pending):
            if p.poll() is not None:
                cluster.stop()
                raise RuntimeError(f"{s['name']} exited during startup (code {p.returncode})"
                                   + (f"; see {log_dir}/{s['name']}.log" if log_dir else ""))
            if _healthy(s["health"]):
                pending.remove((s, p))
        if pending and time.time() > deadline:
            cluster.stop()
            raise RuntimeError("services did not become healthy: "
                               + ", ".join(s["name"] for s, _ in pending))
        if pending:
            time.sleep(0.2)
    return cluster


class LocalCluster:
    """All services on free ports in threads of this process (one telemetry resource)."""

    def __init__(self, data_dir):
        self.servers = []
        val = validation_service.serve(port=0)
        stl = settlement_service.serve(port=0)
        banks = {rt: rdfi_service.serve(rt, port=0) for rt in AUTO_BANKS}
        self.op, op_httpd = operator_server.serve(
            port=0, data_dir=data_dir, validation_url=self._u(val), settlement_url=self._u(stl),
            rdfi_urls={rt: self._u(h) for rt, (_, h) in banks.items()})
        self.operator_url = self._u(op_httpd)
        self.banks = {}
        for rt, (bank, h) in banks.items():
            bank.operator_url = self.operator_url
            self.banks[rt] = bank
        self.servers = [val, stl, *(h for _, h in banks.values()), op_httpd]
        for h in self.servers:
            threading.Thread(target=h.serve_forever, daemon=True).start()

    @staticmethod
    def _u(httpd) -> str:
        return _url(httpd.server_address[1])

    def stop(self):
        for h in self.servers:
            h.shutdown()
            h.server_close()


def start_local(data_dir) -> LocalCluster:
    return LocalCluster(data_dir)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="sim_data")
    ap.add_argument("--no-dashboard", action="store_true")
    ap.add_argument("--log-dir", help="write each service's output to <name>.log here "
                                      "instead of this terminal")
    args = ap.parse_args()
    try:
        cluster = start_processes(args.data_dir, dashboard=not args.no_dashboard,
                                  log_dir=args.log_dir)
    except RuntimeError as exc:
        sys.exit(f"cluster: {exc}")
    print("\nACH simulator running as separate services:", flush=True)
    for s, p in zip(cluster.specs, cluster.procs):
        print(f"  {s['name']:<36} {_url(s['port']):<24} pid {p.pid}", flush=True)
    print(f"\nBanks connect to {cluster.operator_url}. Ctrl+C stops everything.\n", flush=True)

    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    try:
        while cluster.alive():
            time.sleep(0.5)
        dead = [s["name"] for s, p in zip(cluster.specs, cluster.procs) if p.poll() is not None]
        print(f"cluster: {', '.join(dead)} stopped; shutting down the rest", file=sys.stderr)
    except KeyboardInterrupt:
        pass
    finally:
        cluster.stop()
        print("cluster stopped", flush=True)


if __name__ == "__main__":
    main()
