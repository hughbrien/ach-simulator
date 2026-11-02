#!/usr/bin/env python3
"""End-to-end ACH transmission demo.

Starts the simulated network as separate processes (operator, validation,
settlement and one RDFI service per simulated bank; see ach_sim/cluster.py) on
free local ports, then plays a business day
(Monday 2 Nov 2026, Eastern Time):

  Outbound  First Example Bank (ODFI) transmits payroll, B2B debits, consumer WEB
            debits and a Same Day file; a duplicate and a corrupted file are rejected.
  Inbound   Third Demo Credit Union originates payroll and rent to First Example
            Bank's customers. First Example Bank pulls its mailbox (its own returns
            and NOCs plus the inbound file), posts entries to its ledger, and
            transmits returns for what it cannot post.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ach_sim import nacha, telemetry  # noqa: E402
from ach_sim.client import OperatorClient  # noqa: E402
from ach_sim.cluster import start_processes  # noqa: E402
from ach_sim.participants import PARTICIPANTS, FIRST_EXAMPLE_BANK, THIRD_DEMO_CU  # noqa: E402

EX = ROOT / "examples"


def banner(t):
    print("\n" + "=" * 100 + f"\n{t}\n" + "=" * 100)


def show(status, resp):
    print(f"  -> HTTP {status}  {resp.get('status')}  {resp.get('file_id', '')}")
    for b in resp.get("batches", []):
        print(f"     batch {b['batch']} {b['sec']:<3} {b['company']:<16} {b['entries']} entries"
              f"  settles {b['settlement_date']} window {b['window']}")
    for e in resp.get("errors", []):
        print(f"     error: {e}")
    for w in resp.get("warnings", []):
        print(f"     warning: {w}")


def main():
    telemetry.configure_logging(os.environ.get("ACH_SIM_LOG_LEVEL", "WARNING"))
    if telemetry.setup("ach-bank-client"):
        print(f"Telemetry export enabled (metrics, logs, traces) -> {telemetry._state['target']}")
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "sim_data"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    cluster = start_processes(data_dir, free_ports=True, dashboard=False,
                              log_dir=data_dir / "logs")
    try:
        run(cluster, data_dir)
    finally:
        cluster.stop()
        telemetry.flush()


def run(cluster, data_dir):
    url = cluster.operator_url
    print(f"Simulated ACH network running as {len(cluster.procs)} services "
          f"(operator {url}), data in {data_dir}, service logs in {data_dir / 'logs'}")
    for s in cluster.specs:
        print(f"  {s['name']:<36} port {s['port']}")

    feb_key = PARTICIPANTS[FIRST_EXAMPLE_BANK]["api_key"]
    tdcu_key = PARTICIPANTS[THIRD_DEMO_CU]["api_key"]
    morning = OperatorClient(url, feb_key, "2026-11-02T08:30:00-05:00")

    banner("OUTBOUND: First Example Bank transmits its origination files (08:30 ET)")
    for name in ("01_ppd_payroll_credits.ach", "02_ccd_vendor_debits_with_remittance.ach",
                 "03_web_consumer_debits.ach"):
        print(f"\n> send {name}")
        show(*morning.send_file((EX / name).read_text()))

    print("\n> send 04_same_day_ppd_credits.ach at 09:15 ET (before the 10:30 deadline)")
    sd = OperatorClient(url, feb_key, "2026-11-02T09:15:00-05:00")
    show(*sd.send_file((EX / "04_same_day_ppd_credits.ach").read_text()))

    print("\n> send 91_same_day_over_1m_limit.ach (same-day effective date, $1.25M entry)")
    show(*sd.send_file((EX / "91_same_day_over_1m_limit.ach").read_text()))

    print("\n> re-send 01_ppd_payroll_credits.ach (operator duplicate-file check)")
    show(*morning.send_file((EX / "01_ppd_payroll_credits.ach").read_text()))

    print("\n> send 90_INVALID_bad_entry_hash.ach with local pre-check (blocked at the bank)")
    status, resp = morning.send_file((EX / "90_INVALID_bad_entry_hash.ach").read_text())
    print(f"  -> {resp['status']}: {resp['errors']}")
    print("> send it again with the pre-check skipped (rejected by the operator)")
    show(*morning.send_file((EX / "90_INVALID_bad_entry_hash.ach").read_text(), precheck=False))

    banner("INBOUND: Third Demo Credit Union originates to First Example Bank customers")
    tdcu = OperatorClient(url, tdcu_key, "2026-11-02T08:40:00-05:00")
    print("\n> TDCU sends 07_inbound_from_third_demo_cu.ach")
    show(*tdcu.send_file((EX / "07_inbound_from_third_demo_cu.ach").read_text()))

    banner("INBOUND: First Example Bank pulls its mailbox (16:00 ET)")
    _, box = morning.inbox()
    for f in box["files"]:
        print(f"  mailbox: {f['name']}")
    ledger_path = EX / "first_example_bank_ledger.json"
    ledger = json.loads(ledger_path.read_text())
    afternoon = OperatorClient(url, feb_key, "2026-11-02T16:00:00-05:00")
    report = afternoon.receive(ledger, save_dir=data_dir / "first_example_bank_received")
    print("\nPosting results:")
    for f in report["files"]:
        print(f"  {f['name']}")
        for e in f["entries"]:
            print(f"     {e['trace']}  {e['outcome']}")
    if report["returns_sent"]:
        print("\nReturns file transmitted back through the operator:")
        show(*report["returns_sent"])
    print("\nLedger balances after posting:")
    for acct, a in ledger.items():
        print(f"  {acct}  {a['holder']:<30} {nacha.money(a['balance'])}")

    banner("TDCU picks up the returns First Example Bank sent")
    _, box = tdcu.inbox()
    for f in box["files"]:
        _, text = tdcu.download(f["name"])
        parsed = nacha.parse(text)
        if any(e.transaction_code in ("21", "26", "31", "36")
               for b in parsed.batches for e in b.entries):
            print(f"  {f['name']}")
            print(nacha.describe(text))

    banner("Operator event log (sim_data/events.jsonl)")
    counts = {}
    for line in (data_dir / "events.jsonl").read_text().splitlines():
        ev = json.loads(line)["event"]
        counts[ev] = counts.get(ev, 0) + 1
    for k, v in counts.items():
        print(f"  {k:<24} {v}")


if __name__ == "__main__":
    main()
