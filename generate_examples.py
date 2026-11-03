#!/usr/bin/env python3
"""Generate the example Nacha files in ./examples.

The forward files (01-04, 07) are built with the builder API. The return and NOC
files (05, 06) are not hand-written: they come from running the RDFI decision
engine over files 01-03, so every return points at a real trace number.
"""
import copy
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ach_sim import nacha, rdfi  # noqa: E402
from ach_sim.participants import (FIRST_EXAMPLE_BANK as FEB, SECOND_SAMPLE_BANK as SSB,  # noqa: E402
                                  THIRD_DEMO_CU as TDCU, OPERATOR_ROUTING, OPERATOR_NAME,
                                  RDFI_ACCOUNTS)

OUT = Path(__file__).resolve().parent / "examples"
DAY = datetime(2026, 11, 2)      # Monday, a banking business day
TODAY = "261102"                 # Same Day effective date
NEXT = "261103"                  # standard next-day effective date


def outbound_file(minute: int, modifier: str = "A") -> nacha.AchFile:
    return nacha.AchFile(immediate_destination=OPERATOR_ROUTING, immediate_origin=FEB,
                         destination_name=OPERATOR_NAME, origin_name="FIRST EXAMPLE BANK",
                         created=DAY.replace(hour=8, minute=minute),
                         file_id_modifier=modifier)


def ppd_payroll():
    f = outbound_file(5, "A")
    b = f.add_batch(company_name="DEMO PAYROLL CO", company_id="1234567890", sec_code="PPD",
                    entry_description="PAYROLL", descriptive_date="NOV 02",
                    effective_date=NEXT, odfi_routing=FEB)
    b.add_entry("22", SSB, "1000234567", 2_150_75, "EMP-0001", "JANE Q PUBLIC")
    b.add_entry("22", SSB, "1000777001", 1_980_00, "EMP-0002", "OMAR FIELDING")  # -> C05
    b.add_entry("22", SSB, "1000555123", 1_725_40, "EMP-0003", "LENA MARSH")     # -> R02
    b.add_entry("22", TDCU, "55501234", 2_310_10, "EMP-0004", "DEVON PARK")      # -> C01
    b.add_entry("32", TDCU, "55509876", 1_402_88, "EMP-0005", "AISHA BELL",
                addenda_info="NET PAY PERIOD 10/16-10/31 THANK YOU")
    b.add_entry("23", TDCU, "55507777", 0, "EMP-0006", "MARCO DIAZ")             # prenote
    return f


def ccd_vendor_debits():
    f = outbound_file(10, "B")
    b = f.add_batch(company_name="SAMPLE DIST INC", company_id="1987654321", sec_code="CCD",
                    entry_description="INVOICES", effective_date=NEXT, odfi_routing=FEB)
    b.add_entry("27", SSB, "2000188800", 12_500_00, "CUST-ES-0042", "EXAMPLE SUPPLY LLC",
                addenda_info="RMR*IV*INV-10023*12500.00\\")
    b.add_entry("27", SSB, "2000199900", 4_380_25, "CUST-SD-0007", "SAMPLE DESIGN INC",
                addenda_info="RMR*IV*INV-10031*4380.25\\")                       # -> R29
    b.add_entry("27", TDCU, "55600001", 31_000_00, "CUST-DF-0110", "DEMO FREIGHT CO",
                addenda_info="RMR*IV*INV-10044*31000.00\\")
    return f


def web_consumer_debits():
    f = outbound_file(15, "C")
    b = f.add_batch(company_name="SAMPLE MEDIA", company_id="9876500001", sec_code="WEB",
                    entry_description="SUBSCRIPT", effective_date=NEXT, odfi_routing=FEB)
    b.add_entry("27", SSB, "1000234567", 14_99, "SUB-778812", "JANE Q PUBLIC", "R")
    b.add_entry("27", SSB, "3000400401", 89_99, "SUB-778813", "SAM RIVERA", "S")    # -> R01
    b.add_entry("27", SSB, "3000400402", 14_99, "SUB-778814", "PRIYA NAIR", "R")    # -> R10
    b.add_entry("27", SSB, "9999000001", 14_99, "SUB-778815", "CHRIS UNKNOWN", "R")  # -> R03
    return f


def same_day_credits():
    f = outbound_file(20, "D")
    b = f.add_batch(company_name="DEMO PAYROLL CO", company_id="1234567890", sec_code="PPD",
                    entry_description="BONUS", descriptive_date="SD1300",
                    effective_date=TODAY, odfi_routing=FEB)
    b.add_entry("22", SSB, "1000234567", 500_00, "EMP-0001", "JANE Q PUBLIC")
    b.add_entry("22", TDCU, "55507777", 750_00, "EMP-0006", "MARCO DIAZ")
    return f


def inbound_payroll_to_feb():
    """Originated by Third Demo CU's client; lands in First Example Bank's mailbox."""
    f = nacha.AchFile(immediate_destination=OPERATOR_ROUTING, immediate_origin=TDCU,
                      destination_name=OPERATOR_NAME, origin_name="THIRD DEMO CREDIT UNION",
                      created=DAY.replace(hour=7, minute=45), file_id_modifier="A")
    b = f.add_batch(company_name="EXAMPLE CLINIC", company_id="3456789012",
                    sec_code="PPD", entry_description="PAYROLL", effective_date=NEXT,
                    odfi_routing=TDCU)
    b.add_entry("22", FEB, "70001111", 3_250_00, "EC-201", "KIM NGUYEN")
    b.add_entry("22", FEB, "70002222", 2_875_50, "EC-202", "LUIS ORTEGA")
    b.add_entry("22", FEB, "70009999", 1_990_00, "EC-203", "TERRY WALSH")  # no such acct
    b2 = f.add_batch(company_name="EXAMPLE CLINIC", company_id="3456789012",
                     sec_code="CCD", entry_description="RENT", effective_date=NEXT,
                     odfi_routing=TDCU)
    b2.add_entry("27", FEB, "70005000", 8_400_00, "LEASE-17", "BAYSIDE PROPERTIES",
                 addenda_info="RENT NOVEMBER 2026 SUITE 300")
    return f


def bad_hash(text: str) -> str:
    """Corrupt the first batch control's entry hash, as a transmission error would."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("8"):
            lines[i] = ln[:10] + "0000000001" + ln[20:]
            break
    return "\n".join(lines) + "\n"


def over_limit_same_day():
    f = outbound_file(25, "E")
    b = f.add_batch(company_name="DEMO HOLDINGS", company_id="1234567899", sec_code="CCD",
                    entry_description="CASH SWEEP", effective_date=TODAY, odfi_routing=FEB)
    b.add_entry("22", TDCU, "55600001", 1_250_000_00, "SWEEP-01", "DEMO FREIGHT CO")
    return f


def main():
    OUT.mkdir(exist_ok=True)
    files = {
        "01_ppd_payroll_credits.ach": ppd_payroll(),
        "02_ccd_vendor_debits_with_remittance.ach": ccd_vendor_debits(),
        "03_web_consumer_debits.ach": web_consumer_debits(),
        "04_same_day_ppd_credits.ach": same_day_credits(),
        "07_inbound_from_third_demo_cu.ach": inbound_payroll_to_feb(),
        "91_same_day_over_1m_limit.ach": over_limit_same_day(),
    }
    texts = {name: f.to_text() for name, f in files.items()}

    # Run the RDFI decision engine over 01-03 to derive returns and NOCs.
    accounts = copy.deepcopy(RDFI_ACCOUNTS)
    returns, nocs = [], []
    for name in ("01_ppd_payroll_credits.ach", "02_ccd_vendor_debits_with_remittance.ach",
                 "03_web_consumer_debits.ach"):
        parsed = nacha.parse(texts[name])
        for b in parsed.batches:
            for e in b.entries:
                for kind, code, data in rdfi.decide(e, b, accounts[e.rdfi_routing]):
                    if kind == "return":
                        returns.append((e.rdfi_routing, (b, e, kind, code, data)))
                    elif kind == "noc":
                        nocs.append((e.rdfi_routing, (b, e, kind, code, data)))

    # As delivered by the operator to the ODFI. Returns come from SSB only here.
    ret_items = [it for r, it in returns if r == SSB]
    texts["05_returns_R01_R02_R03_R10_R29.ach"] = rdfi.build_response_file(
        ret_items, rdfi_routing=SSB, origin=OPERATOR_ROUTING, origin_name=OPERATOR_NAME,
        destination=FEB, destination_name="FIRST EXAMPLE BANK",
        created=DAY.replace(hour=16, minute=30), file_id_modifier="R").to_text()
    noc_items = [it for r, it in nocs]
    # NOCs from two RDFIs: one file per RDFI in real life; here we take TDCU's C01
    # and SSB's C05 into separate batches by building per RDFI and concatenating batches.
    noc_file = None
    for rdfi_rt in (SSB, TDCU):
        items = [it for r, it in nocs if r == rdfi_rt]
        part = rdfi.build_response_file(
            items, rdfi_routing=rdfi_rt, origin=OPERATOR_ROUTING, origin_name=OPERATOR_NAME,
            destination=FEB, destination_name="FIRST EXAMPLE BANK",
            created=DAY.replace(hour=16, minute=35), file_id_modifier="N")
        if part is None:
            continue
        if noc_file is None:
            noc_file = part
        else:
            for b in part.batches:
                b.batch_number = len(noc_file.batches) + 1
                noc_file.batches.append(b)
    texts["06_notifications_of_change_C01_C05.ach"] = noc_file.to_text()
    texts["90_INVALID_bad_entry_hash.ach"] = bad_hash(texts["01_ppd_payroll_credits.ach"])
    assert len(noc_items) == 2

    for name, text in sorted(texts.items()):
        (OUT / name).write_text(text)
        res = nacha.validate(text)
        status = "valid" if res.ok else f"INVALID ({len(res.errors)} errors)"
        warn = f", {len(res.warnings)} warning(s)" if res.warnings else ""
        print(f"{name:<45} {status}{warn}")


if __name__ == "__main__":
    main()
