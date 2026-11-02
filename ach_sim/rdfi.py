"""RDFI behaviour: decide what happens to each received entry, and build the
return / Notification of Change (NOC) files that go back to the ODFI."""
from __future__ import annotations

from datetime import datetime

from .nacha import (AchFile, Batch, Entry, RETURN_CODE_FOR, RETURN_REASONS, CHANGE_CODES,
                    routing)

CONSUMER_SEC = {"PPD", "WEB", "TEL"}


def decide(entry: Entry, batch: Batch, accounts: dict, apply: bool = True) -> list:
    """Return a list of outcomes for one entry.

    Each outcome is ("post", None, None), ("return", Rxx, info) or ("noc", Cxx, corrected).
    A return replaces posting; a NOC is sent in addition to posting.
    When apply=True the account balance is updated for posted entries.
    """
    acct = accounts.get(entry.account)
    if acct is None:
        return [("return", "R03", "")]
    if acct["status"] == "closed":
        return [("return", "R02", "")]
    if acct["status"] == "frozen":
        return [("return", "R16", "")]
    if not entry.is_credit and not entry.is_prenote:
        if batch.company_id in acct.get("disputes", []):
            code = "R10" if batch.sec_code in CONSUMER_SEC else "R29"
            return [("return", code, "")]
        if entry.amount > acct["balance"]:
            return [("return", "R01", "")]

    outcomes = [("post", None, None)]
    if apply and not entry.is_prenote:
        acct["balance"] += entry.amount if entry.is_credit else -entry.amount

    sent_type = "checking" if entry.transaction_code[0] == "2" else "savings"
    if sent_type != acct["type"]:
        right = ("3" if acct["type"] == "savings" else "2") + entry.transaction_code[1]
        outcomes.append(("noc", "C05", right))
    elif acct.get("noc"):
        outcomes.append(("noc", *acct["noc"]))
    return outcomes


def build_response_file(items, *, rdfi_routing: str, origin: str, origin_name: str,
                        destination: str, destination_name: str, created: datetime,
                        file_id_modifier: str = "A") -> AchFile | None:
    """Build one file of returns and NOCs.

    items: list of (original_batch, original_entry, kind, code, data) where kind is
    "return" or "noc". Returns are grouped into batches that mirror the original
    batch header; NOCs go into COR batches. Trace numbers carry the RDFI's prefix.
    """
    if not items:
        return None
    f = AchFile(immediate_destination=destination, immediate_origin=origin,
                destination_name=destination_name, origin_name=origin_name,
                created=created, file_id_modifier=file_id_modifier)
    groups: dict = {}
    for ob, oe, kind, code, data in items:
        key = (ob.company_id, ob.batch_number, ob.odfi_routing, kind)
        groups.setdefault(key, (ob, kind, []))[2].append((oe, code, data))

    seq = 0
    rdfi8 = rdfi_routing[:8]
    for (_, _, _, kind), (ob, _, rows) in groups.items():
        b = f.add_batch(
            company_name=ob.company_name, company_id=ob.company_id,
            sec_code="COR" if kind == "noc" else ob.sec_code,
            entry_description=ob.entry_description, effective_date=ob.effective_date,
            odfi_routing=rdfi_routing, service_class=ob.service_class,
            descriptive_date=ob.descriptive_date,
        )
        for oe, code, data in rows:
            seq += 1
            trace = f"{rdfi8}{seq:07d}"
            if kind == "return":
                addenda = {"addenda_type_code": "99", "return_reason_code": code,
                           "original_entry_trace_number": oe.trace_number,
                           "original_rdfi_identification": oe.rdfi_routing[:8],
                           "addenda_information": data or ""}
                amount = oe.amount
            else:
                addenda = {"addenda_type_code": "98", "change_code": code,
                           "original_entry_trace_number": oe.trace_number,
                           "original_rdfi_identification": oe.rdfi_routing[:8],
                           "corrected_data": data}
                amount = 0
            b.add_entry(RETURN_CODE_FOR[oe.transaction_code], ob.odfi_routing, oe.account,
                        amount, oe.individual_id, oe.name, oe.discretionary, addenda=[addenda],
                        trace_number=trace)
    return f


def describe_outcome(kind, code, data) -> str:
    if kind == "post":
        return "posted"
    if kind == "return":
        return f"return {code} {RETURN_REASONS.get(code, '')}"
    return f"NOC {code} {CHANGE_CODES.get(code, '')} -> {data}"
