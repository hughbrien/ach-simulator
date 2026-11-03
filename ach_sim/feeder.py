#!/usr/bin/env python3
"""Continuous transaction feed: keep a running simulator busy with random ACH files.

Plays the bank clients against a running operator (start one with
`python3 -m ach_sim.cluster`). Every few seconds First Example Bank originates a
random payroll, vendor-debit, subscription-debit or Same Day file to the two
simulated RDFIs; now and then Third Demo CU originates payroll to First Example
Bank, and a corrupted file is sent to exercise operator rejects. First Example
Bank's mailbox is drained on a timer (posting to a ledger and returning what it
cannot post), so returns and NOCs flow both ways for the whole run.

    python3 -m ach_sim.feeder --duration 3600 --interval 15
    python3 -m ach_sim.feeder --duration 600 --seed 7 --data-dir sim_data

Files use the real clock (no X-Sim-Time), so Same Day windows follow wall time.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import shutil
import signal
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import nacha, telemetry
from .client import OperatorClient
from .participants import (FIRST_EXAMPLE_BANK as FEB, OPERATOR_NAME, OPERATOR_ROUTING,
                           PARTICIPANTS, RDFI_ACCOUNTS, SECOND_SAMPLE_BANK as SSB,
                           THIRD_DEMO_CU as TDCU)
from .service import default_url
from .settlement import ET, current_or_next_business_day, next_business_day

ROOT = Path(__file__).resolve().parents[1]
MODIFIERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
ORIGINATORS = {  # company name, company id (ids in RDFI_ACCOUNTS "disputes" trigger R10/R29)
    "payroll": ("DEMO PAYROLL CO", "1234567890"),
    "vendor": ("SAMPLE DIST INC", "1987654321"),
    "subscription": ("SAMPLE MEDIA", "9876500001"),
    "inbound": ("EXAMPLE CLINIC", "3456789012"),
}
UNKNOWN_ACCOUNT = "9999000001"  # not held at any RDFI -> R03
log = logging.getLogger("ach_sim.feeder")


class Feeder:
    def __init__(self, url: str, rng: random.Random, ledger_path: Path):
        self.rng = rng
        self.feb = OperatorClient(url, PARTICIPANTS[FEB]["api_key"])
        self.tdcu = OperatorClient(url, PARTICIPANTS[TDCU]["api_key"])
        self.ledger_path = ledger_path
        self.sent = Counter()     # per origin, drives the file ID modifier
        self.seq = 0              # individual id / invoice numbering
        self.stats = Counter()
        self.amount = 0

    # -- file building --------------------------------------------------------
    def _file(self, origin: str) -> nacha.AchFile:
        self.sent[origin] += 1
        # Stay clear of the modifiers the banks use for their own return files, so the
        # operator's duplicate check never trips: RDFI services use A-R (so S-9 here),
        # the client's receive uses R.
        mods = "STUVWXYZ0123456789" if origin in RDFI_ACCOUNTS else MODIFIERS.replace("R", "")
        return nacha.AchFile(immediate_destination=OPERATOR_ROUTING, immediate_origin=origin,
                             destination_name=OPERATOR_NAME,
                             origin_name=PARTICIPANTS[origin]["name"],
                             created=datetime.now(ET).replace(tzinfo=None),
                             file_id_modifier=mods[(self.sent[origin] - 1) % len(mods)])

    def _receivers(self, n: int, *, personal: bool | None = None, unknown_rate=0.05):
        """n random (routing, account, holder, type) from the simulated RDFIs."""
        pool = [(rt, acct, a["holder"], a["type"]) for rt, accts in RDFI_ACCOUNTS.items()
                for acct, a in accts.items()
                if personal is None or personal == (not a["holder"].endswith(("LLC", "INC", "CO")))]
        out = self.rng.sample(pool, min(n, len(pool)))
        if self.rng.random() < unknown_rate:
            out.append((SSB, UNKNOWN_ACCOUNT, "CHRIS UNKNOWN", "checking"))
        return out

    def _id(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}-{self.seq:05d}"

    @staticmethod
    def _yymmdd(d) -> str:
        return d.strftime("%y%m%d")

    def payroll(self):
        f = self._file(FEB)
        name, cid = ORIGINATORS["payroll"]
        b = f.add_batch(company_name=name, company_id=cid, sec_code="PPD",
                        entry_description="PAYROLL", effective_date=self._next(),
                        odfi_routing=FEB)
        for rt, acct, holder, typ in self._receivers(self.rng.randint(2, 6), personal=True):
            # usually the right transaction code; sometimes checking-for-savings (-> C05)
            code = "32" if typ == "savings" and self.rng.random() > 0.3 else "22"
            b.add_entry(code, rt, acct, self.rng.randint(800_00, 4_500_00),
                        self._id("EMP"), holder)
        return "payroll", f

    def vendor_debits(self):
        f = self._file(FEB)
        name, cid = ORIGINATORS["vendor"]
        b = f.add_batch(company_name=name, company_id=cid, sec_code="CCD",
                        entry_description="INVOICES", effective_date=self._next(),
                        odfi_routing=FEB)
        for rt, acct, holder, _ in self._receivers(self.rng.randint(1, 3), personal=False):
            amt = self.rng.randint(100_00, 5_000_00)
            inv = self._id("INV")
            b.add_entry("27", rt, acct, amt, inv, holder,
                        addenda_info=f"RMR*IV*{inv}*{amt / 100:.2f}\\")
        return "vendor_debits", f

    def subscriptions(self):
        f = self._file(FEB)
        name, cid = ORIGINATORS["subscription"]
        b = f.add_batch(company_name=name, company_id=cid, sec_code="WEB",
                        entry_description="SUBSCRIPT", effective_date=self._next(),
                        odfi_routing=FEB)
        for rt, acct, holder, _ in self._receivers(self.rng.randint(2, 5), personal=True,
                                                   unknown_rate=0.25):
            b.add_entry("27", rt, acct, self.rng.choice([9_99, 14_99, 19_99, 89_99]),
                        self._id("SUB"), holder, self.rng.choice("RS"))
        return "subscriptions", f

    def same_day(self):
        f = self._file(FEB)
        name, cid = ORIGINATORS["payroll"]
        today = current_or_next_business_day(datetime.now(ET).date())
        b = f.add_batch(company_name=name, company_id=cid, sec_code="PPD",
                        entry_description="BONUS", effective_date=self._yymmdd(today),
                        odfi_routing=FEB)
        for rt, acct, holder, _ in self._receivers(self.rng.randint(1, 3), personal=True,
                                                   unknown_rate=0):
            b.add_entry("22", rt, acct, self.rng.randint(100_00, 1_000_00), self._id("EMP"),
                        holder)
        return "same_day", f

    def inbound(self):
        """Third Demo CU pays First Example Bank customers (lands in FEB's mailbox)."""
        f = self._file(TDCU)
        name, cid = ORIGINATORS["inbound"]
        b = f.add_batch(company_name=name, company_id=cid, sec_code="PPD",
                        entry_description="PAYROLL", effective_date=self._next(),
                        odfi_routing=TDCU)
        accounts = json.loads(self.ledger_path.read_text())
        people = [(a, v["holder"]) for a, v in accounts.items() if "CLINIC" not in v["holder"]]
        if self.rng.random() < 0.2:
            people.append(("70009999", "TERRY WALSH"))  # no such account -> R03 from FEB
        for acct, holder in people:
            b.add_entry("22", FEB, acct, self.rng.randint(1_500_00, 4_000_00),
                        self._id("EC"), holder)
        return "inbound", f

    def _next(self) -> str:
        return self._yymmdd(next_business_day(datetime.now(ET).date()))

    SCENARIOS = [("payroll", 30), ("vendor_debits", 25), ("subscriptions", 25),
                 ("same_day", 10), ("inbound", 10)]

    # -- sending ----------------------------------------------------------------
    def tick(self, corrupt_rate: float):
        names, weights = zip(*self.SCENARIOS)
        kind, f = getattr(self, self.rng.choices(names, weights)[0])()
        text = f.to_text()
        client = self.tdcu if f.immediate_origin == TDCU else self.feb
        corrupt = self.rng.random() < corrupt_rate
        if corrupt:  # a transmission error the operator must catch
            lines = text.splitlines()
            i = next(i for i, ln in enumerate(lines) if ln.startswith("8"))
            lines[i] = lines[i][:10] + "0000000001" + lines[i][20:]
            text, kind = "\n".join(lines) + "\n", kind + "+corrupt"
        status, ack = client.send_file(text, precheck=not corrupt)
        entries = sum(len(b.entries) for b in f.batches)
        total = sum(e.amount for b in f.batches for e in b.entries)
        ack = ack if isinstance(ack, dict) else {}
        self.stats[f"http_{status}"] += 1
        self.stats["entries"] += entries if status == 200 else 0
        self.amount += total if status == 200 else 0
        windows = ",".join(sorted({b.get("window", "") for b in ack.get("batches", [])}))
        print(f"{datetime.now(ET):%H:%M:%S}  {client.bank_name:<24} {kind:<22} "
              f"{ack.get('file_id', '-'):<16} HTTP {status}  {entries:>2} entries "
              f"${total / 100:>11,.2f}  {windows}", flush=True)

    def drain_mailbox(self):
        """First Example Bank pulls its mailbox, posts to its ledger, sends returns."""
        ledger = json.loads(self.ledger_path.read_text())
        report = self.feb.receive(ledger, save_dir=None, verbose=False)
        self.ledger_path.write_text(json.dumps(ledger, indent=2))
        n = len(report.get("files", []))
        self.stats["mailbox_files"] += n
        if n:
            print(f"{datetime.now(ET):%H:%M:%S}  FIRST EXAMPLE BANK       mailbox: {n} file(s) "
                  f"processed" + (", returns sent" if report.get("returns_sent") else ""),
                  flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=default_url("operator"))
    ap.add_argument("--duration", type=float, default=3600, help="seconds (default 3600)")
    ap.add_argument("--interval", type=float, default=15,
                    help="mean seconds between files (default 15)")
    ap.add_argument("--receive-every", type=float, default=60,
                    help="seconds between First Example Bank mailbox pulls (default 60)")
    ap.add_argument("--corrupt-rate", type=float, default=0.03,
                    help="share of files sent with a broken entry hash (default 0.03)")
    ap.add_argument("--data-dir", default="sim_data",
                    help="where First Example Bank's working ledger is kept")
    ap.add_argument("--seed", type=int)
    args = ap.parse_args()

    telemetry.configure_logging(os.environ.get("ACH_SIM_LOG_LEVEL", "WARNING"))
    if telemetry.setup("ach-bank-client"):
        print(f"Telemetry export enabled -> {telemetry._state["target"]}", flush=True)
    ledger = Path(args.data_dir) / "first_example_bank_ledger.json"
    if not ledger.exists():
        ledger.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "examples" / "first_example_bank_ledger.json", ledger)

    feeder = Feeder(args.url, random.Random(args.seed), ledger)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    start = time.monotonic()
    end, next_pull = start + args.duration, start + args.receive_every
    print(f"Feeding {args.url} for {args.duration:.0f}s, one file every "
          f"~{args.interval:g}s. Ctrl+C stops.", flush=True)
    try:
        while time.monotonic() < end:
            try:
                feeder.tick(args.corrupt_rate)
                if time.monotonic() >= next_pull:
                    feeder.drain_mailbox()
                    next_pull = time.monotonic() + args.receive_every
            except OSError as exc:  # operator down or restarting: keep going
                feeder.stats["errors"] += 1
                print(f"{datetime.now(ET):%H:%M:%S}  operator unreachable: {exc}", flush=True)
            time.sleep(min(max(0.5, feeder.rng.expovariate(1 / args.interval)),
                           max(0, end - time.monotonic())))
        feeder.drain_mailbox()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        mins = (time.monotonic() - start) / 60
        s = feeder.stats
        print(f"\nFed for {mins:.1f} min: {s['http_200']} files accepted, "
              f"{sum(v for k, v in s.items() if k.startswith('http_') and k != 'http_200')} "
              f"rejected, {s['entries']} entries, ${feeder.amount / 100:,.2f}, "
              f"{s['mailbox_files']} mailbox files processed, {s['errors']} errors", flush=True)
        telemetry.flush()


if __name__ == "__main__":
    main()
