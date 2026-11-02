"""Nacha (ACH) file format: build, parse and validate.

Covers the six record types of a domestic ACH file:

    1  File Header        5  Company/Batch Header     6  Entry Detail
    7  Addenda (05 / 98 / 99)                         8  Batch Control
    9  File Control (plus "9999..." block padding)

Every record is exactly 94 characters. Files are blocked in groups of 10
records; the last block is filled with records of all 9s.

Standard library only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime

RECORD_LEN = 94
BLOCKING_FACTOR = 10
PADDING_RECORD = "9" * RECORD_LEN
SAME_DAY_LIMIT_CENTS = 1_000_000_00  # $1,000,000 per entry

# ---------------------------------------------------------------------------
# Field layouts: (name, start, end, kind)  -- positions are 1-based inclusive
#   N = numeric, right-justified, zero-filled
#   A = alphanumeric, left-justified, space-filled
#   R = alphanumeric, right-justified, space-filled (immediate dest/origin)
# ---------------------------------------------------------------------------
FILE_HEADER = [
    ("record_type", 1, 1, "A"),
    ("priority_code", 2, 3, "N"),
    ("immediate_destination", 4, 13, "R"),
    ("immediate_origin", 14, 23, "R"),
    ("file_creation_date", 24, 29, "N"),
    ("file_creation_time", 30, 33, "N"),
    ("file_id_modifier", 34, 34, "A"),
    ("record_size", 35, 37, "N"),
    ("blocking_factor", 38, 39, "N"),
    ("format_code", 40, 40, "N"),
    ("immediate_destination_name", 41, 63, "A"),
    ("immediate_origin_name", 64, 86, "A"),
    ("reference_code", 87, 94, "A"),
]

BATCH_HEADER = [
    ("record_type", 1, 1, "A"),
    ("service_class_code", 2, 4, "N"),
    ("company_name", 5, 20, "A"),
    ("company_discretionary_data", 21, 40, "A"),
    ("company_identification", 41, 50, "A"),
    ("sec_code", 51, 53, "A"),
    ("company_entry_description", 54, 63, "A"),
    ("company_descriptive_date", 64, 69, "A"),
    ("effective_entry_date", 70, 75, "A"),
    ("settlement_date", 76, 78, "A"),  # Julian day, inserted by the ACH Operator
    ("originator_status_code", 79, 79, "A"),
    ("odfi_identification", 80, 87, "N"),
    ("batch_number", 88, 94, "N"),
]

ENTRY_DETAIL = [
    ("record_type", 1, 1, "A"),
    ("transaction_code", 2, 3, "N"),
    ("rdfi_routing", 4, 11, "N"),
    ("check_digit", 12, 12, "N"),
    ("dfi_account_number", 13, 29, "A"),
    ("amount", 30, 39, "N"),
    ("individual_id", 40, 54, "A"),       # CCD: identification number
    ("individual_name", 55, 76, "A"),     # CCD: receiving company name
    ("discretionary_data", 77, 78, "A"),  # WEB/TEL: payment type code
    ("addenda_record_indicator", 79, 79, "N"),
    ("trace_number", 80, 94, "N"),
]

ADDENDA_05 = [
    ("record_type", 1, 1, "A"),
    ("addenda_type_code", 2, 3, "N"),
    ("payment_related_information", 4, 83, "A"),
    ("addenda_sequence_number", 84, 87, "N"),
    ("entry_detail_sequence_number", 88, 94, "N"),
]

ADDENDA_98 = [  # Notification of Change (COR)
    ("record_type", 1, 1, "A"),
    ("addenda_type_code", 2, 3, "N"),
    ("change_code", 4, 6, "A"),
    ("original_entry_trace_number", 7, 21, "N"),
    ("reserved_1", 22, 27, "A"),
    ("original_rdfi_identification", 28, 35, "N"),
    ("corrected_data", 36, 64, "A"),
    ("reserved_2", 65, 79, "A"),
    ("trace_number", 80, 94, "N"),
]

ADDENDA_99 = [  # Return
    ("record_type", 1, 1, "A"),
    ("addenda_type_code", 2, 3, "N"),
    ("return_reason_code", 4, 6, "A"),
    ("original_entry_trace_number", 7, 21, "N"),
    ("date_of_death", 22, 27, "A"),
    ("original_rdfi_identification", 28, 35, "N"),
    ("addenda_information", 36, 79, "A"),
    ("trace_number", 80, 94, "N"),
]

BATCH_CONTROL = [
    ("record_type", 1, 1, "A"),
    ("service_class_code", 2, 4, "N"),
    ("entry_addenda_count", 5, 10, "N"),
    ("entry_hash", 11, 20, "N"),
    ("total_debit_amount", 21, 32, "N"),
    ("total_credit_amount", 33, 44, "N"),
    ("company_identification", 45, 54, "A"),
    ("message_authentication_code", 55, 73, "A"),
    ("reserved", 74, 79, "A"),
    ("odfi_identification", 80, 87, "N"),
    ("batch_number", 88, 94, "N"),
]

FILE_CONTROL = [
    ("record_type", 1, 1, "A"),
    ("batch_count", 2, 7, "N"),
    ("block_count", 8, 13, "N"),
    ("entry_addenda_count", 14, 21, "N"),
    ("entry_hash", 22, 31, "N"),
    ("total_debit_amount", 32, 43, "N"),
    ("total_credit_amount", 44, 55, "N"),
    ("reserved", 56, 94, "A"),
]

ADDENDA_LAYOUTS = {"05": ADDENDA_05, "98": ADDENDA_98, "99": ADDENDA_99}

# ---------------------------------------------------------------------------
# Reference tables
# ---------------------------------------------------------------------------
TRANSACTION_CODES = {
    "22": "Checking credit", "23": "Checking credit prenote",
    "27": "Checking debit", "28": "Checking debit prenote",
    "32": "Savings credit", "33": "Savings credit prenote",
    "37": "Savings debit", "38": "Savings debit prenote",
    "21": "Return/NOC of checking credit", "26": "Return/NOC of checking debit",
    "31": "Return/NOC of savings credit", "36": "Return/NOC of savings debit",
}
PRENOTE_CODES = {"23", "28", "33", "38"}
RETURN_CODE_FOR = {"22": "21", "23": "21", "27": "26", "28": "26",
                   "32": "31", "33": "31", "37": "36", "38": "36"}

SERVICE_CLASS = {"200": "Mixed debits and credits", "220": "Credits only",
                 "225": "Debits only"}

SEC_CODES = {
    "PPD": "Prearranged Payment and Deposit (consumer)",
    "CCD": "Corporate Credit or Debit (business to business)",
    "WEB": "Internet-initiated / mobile entry (consumer debit)",
    "TEL": "Telephone-initiated entry (consumer debit)",
    "COR": "Notification of Change / refused NOC",
}

RETURN_REASONS = {
    "R01": "Insufficient Funds",
    "R02": "Account Closed",
    "R03": "No Account/Unable to Locate Account",
    "R04": "Invalid Account Number Structure",
    "R13": "Invalid ACH Routing Number",
    "R10": "Customer Advises Originator is Not Known and/or Not Authorized",
    "R16": "Account Frozen/Entry Returned per OFAC Instruction",
    "R29": "Corporate Customer Advises Not Authorized",
}
CHANGE_CODES = {
    "C01": "Incorrect DFI Account Number",
    "C02": "Incorrect Routing Number",
    "C05": "Incorrect Transaction Code",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def aba_check_digit(first8: str) -> str:
    """ABA routing check digit (weights 3,7,1 repeating)."""
    if len(first8) != 8 or not first8.isdigit():
        raise ValueError(f"need 8 digits, got {first8!r}")
    weights = (3, 7, 1) * 3
    total = sum(int(d) * w for d, w in zip(first8, weights))
    return str((10 - total % 10) % 10)


def routing(first8: str) -> str:
    """Return the full 9-digit routing number for an 8-digit prefix."""
    return first8 + aba_check_digit(first8)


def valid_routing(r9: str) -> bool:
    return len(r9) == 9 and r9.isdigit() and aba_check_digit(r9[:8]) == r9[8]


def is_credit(transaction_code: str) -> bool:
    """Second digit 0-4 = credit, 5-9 = debit."""
    return int(transaction_code[1]) <= 4


def format_record(layout, values: dict) -> str:
    out = []
    for name, start, end, kind in layout:
        width = end - start + 1
        v = values.get(name, "")
        v = "" if v is None else str(v)
        if kind == "N":
            v = v.strip() or "0"
            if not v.isdigit():
                raise ValueError(f"{name} must be numeric, got {v!r}")
            if len(v) > width:
                v = v[-width:] if name.endswith("hash") else _too_long(name, v, width)
            out.append(v.rjust(width, "0"))
        else:
            v = v.upper()
            if len(v) > width:
                v = v[:width]
            out.append(v.rjust(width) if kind == "R" else v.ljust(width))
    rec = "".join(out)
    assert len(rec) == RECORD_LEN, (layout[0], len(rec))
    return rec


def _too_long(name, v, width):
    raise ValueError(f"{name} value {v!r} exceeds {width} digits")


def parse_record(layout, line: str) -> dict:
    vals = {}
    for name, start, end, kind in layout:
        raw = line[start - 1:end]
        vals[name] = raw.strip() if kind in "AR" else raw
    return vals


def _full_routing(first8: str) -> str:
    """Records carry only 8 digits of the ODFI routing; rebuild the 9th."""
    return routing(first8) if first8.isdigit() and len(first8) == 8 else first8


def julian_day(d: date) -> str:
    return f"{d.timetuple().tm_yday:03d}"


def yymmdd(d: date) -> str:
    return d.strftime("%y%m%d")


def parse_yymmdd(s: str) -> date:
    return datetime.strptime(s, "%y%m%d").date()


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


# ---------------------------------------------------------------------------
# Object model
# ---------------------------------------------------------------------------
@dataclass
class Entry:
    transaction_code: str
    rdfi_routing: str            # full 9-digit routing number
    account: str
    amount: int                  # cents
    individual_id: str = ""
    name: str = ""
    discretionary: str = ""
    trace_number: str = ""       # assigned at build time if blank
    addenda: list = field(default_factory=list)  # list[dict] using ADDENDA_* fields

    @property
    def is_credit(self) -> bool:
        return is_credit(self.transaction_code)

    @property
    def is_prenote(self) -> bool:
        return self.transaction_code in PRENOTE_CODES

    def record(self) -> str:
        return format_record(ENTRY_DETAIL, {
            "record_type": "6",
            "transaction_code": self.transaction_code,
            "rdfi_routing": self.rdfi_routing[:8],
            "check_digit": self.rdfi_routing[8],
            "dfi_account_number": self.account,
            "amount": self.amount,
            "individual_id": self.individual_id,
            "individual_name": self.name,
            "discretionary_data": self.discretionary,
            "addenda_record_indicator": "1" if self.addenda else "0",
            "trace_number": self.trace_number,
        })


@dataclass
class Batch:
    company_name: str
    company_id: str
    sec_code: str
    entry_description: str
    effective_date: str          # YYMMDD
    odfi_routing: str            # full 9-digit routing (first 8 go in the record)
    service_class: str = ""      # auto-derived when blank
    descriptive_date: str = ""
    discretionary: str = ""
    settlement_date: str = ""    # Julian; left blank by originators
    originator_status: str = "1"
    batch_number: int = 0
    entries: list = field(default_factory=list)

    def add_entry(self, transaction_code, rdfi_routing, account, amount, individual_id="",
                  name="", discretionary="", addenda_info=None, addenda=None, trace_number=""):
        """Add an entry. `addenda_info` (str) creates one 05 payment-related addenda."""
        recs = list(addenda or [])
        if addenda_info:
            recs.append({"addenda_type_code": "05",
                         "payment_related_information": addenda_info})
        e = Entry(transaction_code, rdfi_routing, account, int(amount), individual_id, name,
                  discretionary, trace_number, recs)
        self.entries.append(e)
        return e

    # -- derived values ----------------------------------------------------
    def derived_service_class(self) -> str:
        if self.service_class:
            return self.service_class
        kinds = {e.is_credit for e in self.entries}
        if kinds == {True}:
            return "220"
        if kinds == {False}:
            return "225"
        return "200"

    def totals(self):
        debit = sum(e.amount for e in self.entries if not e.is_credit)
        credit = sum(e.amount for e in self.entries if e.is_credit)
        return debit, credit

    def entry_hash(self) -> int:
        return sum(int(e.rdfi_routing[:8]) for e in self.entries)

    def entry_addenda_count(self) -> int:
        return sum(1 + len(e.addenda) for e in self.entries)

    def assign_traces(self, start: int = 0) -> int:
        """Give blank entries trace numbers (ODFI id + sequence). Returns the last
        sequence used so the next batch in the file continues from it; trace
        numbers must be unique within a file."""
        seq = start
        for e in self.entries:
            if not e.trace_number:
                seq += 1
                e.trace_number = f"{self.odfi_routing[:8]}{seq:07d}"
            else:
                seq = max(seq, int(e.trace_number[-7:]))
        return seq

    def records(self) -> list[str]:
        self.assign_traces()
        sc = self.derived_service_class()
        out = [format_record(BATCH_HEADER, {
            "record_type": "5", "service_class_code": sc,
            "company_name": self.company_name,
            "company_discretionary_data": self.discretionary,
            "company_identification": self.company_id,
            "sec_code": self.sec_code,
            "company_entry_description": self.entry_description,
            "company_descriptive_date": self.descriptive_date,
            "effective_entry_date": self.effective_date,
            "settlement_date": self.settlement_date,
            "originator_status_code": self.originator_status,
            "odfi_identification": self.odfi_routing[:8],
            "batch_number": self.batch_number,
        })]
        for e in self.entries:
            out.append(e.record())
            for i, a in enumerate(e.addenda, start=1):
                code = a["addenda_type_code"]
                vals = dict(a, record_type="7")
                if code == "05":
                    vals.setdefault("addenda_sequence_number", i)
                    vals["entry_detail_sequence_number"] = e.trace_number[-7:]
                else:  # 98 / 99 carry the entry's own trace number
                    vals["trace_number"] = e.trace_number
                out.append(format_record(ADDENDA_LAYOUTS[code], vals))
        debit, credit = self.totals()
        out.append(format_record(BATCH_CONTROL, {
            "record_type": "8", "service_class_code": sc,
            "entry_addenda_count": self.entry_addenda_count(),
            "entry_hash": str(self.entry_hash())[-10:],
            "total_debit_amount": debit, "total_credit_amount": credit,
            "company_identification": self.company_id,
            "odfi_identification": self.odfi_routing[:8],
            "batch_number": self.batch_number,
        }))
        return out


@dataclass
class AchFile:
    immediate_destination: str   # 9-digit routing of the receiving point
    immediate_origin: str        # 9-digit routing (or 10-char company id) of the sender
    destination_name: str
    origin_name: str
    created: datetime
    file_id_modifier: str = "A"
    reference_code: str = ""
    batches: list = field(default_factory=list)

    def add_batch(self, **kwargs) -> Batch:
        b = Batch(**kwargs)
        b.batch_number = b.batch_number or len(self.batches) + 1
        self.batches.append(b)
        return b

    @property
    def file_key(self) -> str:
        """Operator duplicate-detection key."""
        return (f"{self.immediate_origin}-{self.created:%y%m%d}-{self.created:%H%M}"
                f"-{self.file_id_modifier}")

    def to_text(self) -> str:
        lines = [format_record(FILE_HEADER, {
            "record_type": "1", "priority_code": "01",
            "immediate_destination": " " + self.immediate_destination,
            "immediate_origin": " " + self.immediate_origin
            if len(self.immediate_origin) == 9 else self.immediate_origin,
            "file_creation_date": self.created.strftime("%y%m%d"),
            "file_creation_time": self.created.strftime("%H%M"),
            "file_id_modifier": self.file_id_modifier,
            "record_size": "094", "blocking_factor": "10", "format_code": "1",
            "immediate_destination_name": self.destination_name,
            "immediate_origin_name": self.origin_name,
            "reference_code": self.reference_code,
        })]
        seq = 0
        for b in self.batches:
            seq = b.assign_traces(seq)
        for b in self.batches:
            lines.extend(b.records())
        count = sum(b.entry_addenda_count() for b in self.batches)
        ehash = sum(b.entry_hash() for b in self.batches)
        debit = sum(b.totals()[0] for b in self.batches)
        credit = sum(b.totals()[1] for b in self.batches)
        n_records = len(lines) + 1
        blocks = math.ceil(n_records / BLOCKING_FACTOR)
        lines.append(format_record(FILE_CONTROL, {
            "record_type": "9", "batch_count": len(self.batches), "block_count": blocks,
            "entry_addenda_count": count, "entry_hash": str(ehash)[-10:],
            "total_debit_amount": debit, "total_credit_amount": credit,
        }))
        lines.extend([PADDING_RECORD] * (blocks * BLOCKING_FACTOR - len(lines)))
        return "\n".join(lines) + "\n"

    def summary(self) -> dict:
        n = sum(len(b.entries) for b in self.batches)
        debit = sum(b.totals()[0] for b in self.batches)
        credit = sum(b.totals()[1] for b in self.batches)
        return {"origin": self.immediate_origin, "destination": self.immediate_destination,
                "batches": len(self.batches), "entries": n,
                "total_debit_cents": debit, "total_credit_cents": credit}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
class NachaParseError(Exception):
    pass


def parse(text: str) -> AchFile:
    """Parse Nacha text into the object model (no rule validation; see validate())."""
    lines = [ln.rstrip("\r\n") for ln in text.splitlines() if ln.strip()]
    if not lines or lines[0][:1] != "1":
        raise NachaParseError("file must start with a type 1 File Header record")
    for i, ln in enumerate(lines, 1):
        if len(ln) != RECORD_LEN:
            raise NachaParseError(f"line {i}: record is {len(ln)} chars, expected 94")
    h = parse_record(FILE_HEADER, lines[0])
    f = AchFile(
        immediate_destination=h["immediate_destination"],
        immediate_origin=h["immediate_origin"],
        destination_name=h["immediate_destination_name"],
        origin_name=h["immediate_origin_name"],
        created=datetime.strptime(h["file_creation_date"] + h["file_creation_time"],
                                  "%y%m%d%H%M"),
        file_id_modifier=h["file_id_modifier"],
        reference_code=h["reference_code"],
    )
    batch = None
    entry = None
    for i, ln in enumerate(lines[1:], 2):
        t = ln[0]
        if t == "5":
            bh = parse_record(BATCH_HEADER, ln)
            batch = Batch(
                company_name=bh["company_name"], company_id=bh["company_identification"],
                sec_code=bh["sec_code"], entry_description=bh["company_entry_description"],
                effective_date=bh["effective_entry_date"],
                odfi_routing=_full_routing(bh["odfi_identification"]),
                service_class=bh["service_class_code"],
                descriptive_date=bh["company_descriptive_date"],
                discretionary=bh["company_discretionary_data"],
                settlement_date=bh["settlement_date"],
                originator_status=bh["originator_status_code"],
                batch_number=int(bh["batch_number"]),
            )
            f.batches.append(batch)
            entry = None
        elif t == "6":
            if batch is None:
                raise NachaParseError(f"line {i}: entry outside a batch")
            ed = parse_record(ENTRY_DETAIL, ln)
            entry = Entry(ed["transaction_code"], ed["rdfi_routing"] + ed["check_digit"],
                          ed["dfi_account_number"], int(ed["amount"]), ed["individual_id"],
                          ed["individual_name"], ed["discretionary_data"], ed["trace_number"])
            entry._addenda_indicator = ed["addenda_record_indicator"]
            batch.entries.append(entry)
        elif t == "7":
            if entry is None:
                raise NachaParseError(f"line {i}: addenda without a preceding entry")
            code = ln[1:3]
            if code not in ADDENDA_LAYOUTS:
                raise NachaParseError(f"line {i}: unsupported addenda type {code}")
            entry.addenda.append(parse_record(ADDENDA_LAYOUTS[code], ln))
        elif t == "8":
            if batch is None:
                raise NachaParseError(f"line {i}: batch control without batch header")
            batch._control = parse_record(BATCH_CONTROL, ln)
            batch = entry = None
        elif t == "9":
            if ln == PADDING_RECORD and hasattr(f, "_control"):
                continue
            f._control = parse_record(FILE_CONTROL, ln)
        else:
            raise NachaParseError(f"line {i}: unknown record type {t!r}")
    if not hasattr(f, "_control"):
        raise NachaParseError("missing type 9 File Control record")
    f._raw_lines = lines
    return f


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
@dataclass
class ValidationResult:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate(text: str) -> ValidationResult:
    """Structural and rule checks an ODFI or ACH Operator would apply to a file."""
    r = ValidationResult()
    raw = text.splitlines()
    if any(len(ln) != RECORD_LEN for ln in raw if ln):
        bad = [i for i, ln in enumerate(raw, 1) if ln and len(ln) != RECORD_LEN]
        r.errors.append(f"records not 94 characters at lines {bad[:10]}")
        return r
    try:
        f = parse(text)
    except NachaParseError as e:
        r.errors.append(str(e))
        return r
    lines = f._raw_lines
    h = parse_record(FILE_HEADER, lines[0])

    # --- file header -------------------------------------------------------
    if h["priority_code"] != "01":
        r.errors.append("file header: priority code must be 01")
    if h["record_size"] != "094" or h["blocking_factor"] != "10" or h["format_code"] != "1":
        r.errors.append("file header: record size/blocking factor/format code must be 094/10/1")
    if not valid_routing(f.immediate_destination):
        r.errors.append(f"file header: immediate destination {f.immediate_destination} "
                        "fails ABA check digit")
    if not (f.file_id_modifier.isalnum() and len(f.file_id_modifier) == 1):
        r.errors.append("file header: file ID modifier must be A-Z or 0-9")

    # --- blocking ----------------------------------------------------------
    if len(lines) % BLOCKING_FACTOR:
        r.errors.append(f"file has {len(lines)} records; must be a multiple of 10")
    fc_idx = next(i for i, ln in enumerate(lines) if ln[0] == "9" and ln != PADDING_RECORD)
    if any(ln != PADDING_RECORD for ln in lines[fc_idx + 1:]):
        r.errors.append("records after the File Control must be all-9 padding")

    # --- record ordering ---------------------------------------------------
    allowed_next = {"1": "58", "5": "6", "6": "678", "7": "678", "8": "59"}
    for i in range(fc_idx):
        t, nxt = lines[i][0], lines[i + 1][0]
        if nxt not in allowed_next.get(t, ""):
            r.errors.append(f"line {i + 2}: record type {nxt} may not follow type {t}")

    # --- batches -----------------------------------------------------------
    created = f.created.date()
    seen_batch_numbers = set()
    seen_traces = set()
    for b in f.batches:
        label = f"batch {b.batch_number} ({b.sec_code} {b.company_name})"
        ctl = getattr(b, "_control", None)
        if ctl is None:
            r.errors.append(f"{label}: missing batch control")
            continue
        if b.batch_number in seen_batch_numbers:
            r.errors.append(f"{label}: duplicate batch number")
        seen_batch_numbers.add(b.batch_number)
        if b.service_class not in SERVICE_CLASS:
            r.errors.append(f"{label}: invalid service class {b.service_class}")
        if b.sec_code not in SEC_CODES:
            r.errors.append(f"{label}: unsupported SEC code {b.sec_code}")
        if not b.entries:
            r.errors.append(f"{label}: batch has no entries")
        try:
            eff = parse_yymmdd(b.effective_date)
        except ValueError:
            r.errors.append(f"{label}: effective entry date {b.effective_date!r} is not YYMMDD")
            eff = None
        odfi8 = b.odfi_routing[:8]

        # entry level
        prev_trace = 0
        for e in b.entries:
            el = f"{label} trace {e.trace_number}"
            if e.transaction_code not in TRANSACTION_CODES:
                r.errors.append(f"{el}: unknown transaction code {e.transaction_code}")
                continue
            if not valid_routing(e.rdfi_routing):
                r.errors.append(f"{el}: RDFI routing {e.rdfi_routing} fails ABA check digit")
            if not e.account:
                r.errors.append(f"{el}: DFI account number is blank")
            if e.is_prenote and e.amount != 0:
                r.errors.append(f"{el}: prenote must have a zero amount")
            if b.sec_code != "COR" and not e.is_prenote and e.amount == 0:
                r.errors.append(f"{el}: zero-dollar live entry")
            sc = b.service_class
            if sc == "220" and not e.is_credit:
                r.errors.append(f"{el}: debit in a credits-only (220) batch")
            if sc == "225" and e.is_credit:
                r.errors.append(f"{el}: credit in a debits-only (225) batch")
            if e.trace_number[:8] != odfi8:
                r.errors.append(f"{el}: trace prefix must equal batch ODFI id {odfi8}")
            if e.trace_number in seen_traces:
                r.errors.append(f"{el}: duplicate trace number in file")
            seen_traces.add(e.trace_number)
            seq = int(e.trace_number)
            if seq <= prev_trace:
                r.errors.append(f"{el}: trace numbers must ascend within a batch")
            prev_trace = seq
            expect_ind = "1" if e.addenda else "0"
            if getattr(e, "_addenda_indicator", expect_ind) != expect_ind:
                r.errors.append(f"{el}: addenda indicator does not match addenda present")
            is_return = e.transaction_code in ("21", "26", "31", "36")
            if b.sec_code in ("WEB", "TEL") and not e.is_credit and not is_return and \
                    e.discretionary not in ("R", "S"):
                r.errors.append(f"{el}: {b.sec_code} debit needs payment type code R or S")
            types = [a["addenda_type_code"] for a in e.addenda]
            if b.sec_code == "COR":
                if types != ["98"]:
                    r.errors.append(f"{el}: COR entry needs exactly one 98 addenda")
            elif is_return:
                if types != ["99"]:
                    r.errors.append(f"{el}: return entry needs exactly one 99 addenda")
            elif any(t != "05" for t in types):
                r.errors.append(f"{el}: forward entry may only carry 05 addenda")
            elif len(types) > 1:
                r.errors.append(f"{el}: {b.sec_code} allows at most one addenda")
            for idx, a in enumerate(e.addenda, 1):
                if a["addenda_type_code"] == "05":
                    if int(a["entry_detail_sequence_number"]) != int(e.trace_number[-7:]):
                        r.errors.append(f"{el}: addenda entry sequence number mismatch")
                    if int(a["addenda_sequence_number"]) != idx:
                        r.errors.append(f"{el}: addenda sequence number should be {idx}")
                elif a["addenda_type_code"] == "99":
                    if a["return_reason_code"] not in RETURN_REASONS:
                        r.warnings.append(f"{el}: return code {a['return_reason_code']} "
                                          "not in simulator table")
                elif a["addenda_type_code"] == "98":
                    if a["change_code"] not in CHANGE_CODES:
                        r.warnings.append(f"{el}: change code {a['change_code']} "
                                          "not in simulator table")
            # Same Day eligibility
            if eff is not None and eff == created and e.amount > SAME_DAY_LIMIT_CENTS:
                r.warnings.append(f"{el}: {money(e.amount)} exceeds the $1M Same Day "
                                  "per-payment limit; not Same Day eligible")

        # batch control
        debit, credit = b.totals()
        checks = [
            ("service class", ctl["service_class_code"], b.service_class),
            ("entry/addenda count", int(ctl["entry_addenda_count"]), b.entry_addenda_count()),
            ("entry hash", int(ctl["entry_hash"]), b.entry_hash() % 10**10),
            ("total debit", int(ctl["total_debit_amount"]), debit),
            ("total credit", int(ctl["total_credit_amount"]), credit),
            ("company id", ctl["company_identification"], b.company_id),
            ("ODFI id", ctl["odfi_identification"], odfi8),
            ("batch number", int(ctl["batch_number"]), b.batch_number),
        ]
        for name, got, want in checks:
            if got != want:
                r.errors.append(f"{label}: batch control {name} is {got}, computed {want}")

    # --- file control ------------------------------------------------------
    fc = f._control
    n_records_before_pad = fc_idx + 1
    checks = [
        ("batch count", int(fc["batch_count"]), len(f.batches)),
        ("block count", int(fc["block_count"]), math.ceil(n_records_before_pad / 10)),
        ("entry/addenda count", int(fc["entry_addenda_count"]),
         sum(b.entry_addenda_count() for b in f.batches)),
        ("entry hash", int(fc["entry_hash"]), sum(b.entry_hash() for b in f.batches) % 10**10),
        ("total debit", int(fc["total_debit_amount"]), sum(b.totals()[0] for b in f.batches)),
        ("total credit", int(fc["total_credit_amount"]), sum(b.totals()[1] for b in f.batches)),
    ]
    for name, got, want in checks:
        if got != want:
            r.errors.append(f"file control {name} is {got}, computed {want}")
    return r


def describe(text: str) -> str:
    """Human-readable dump of a file, for the CLI and README."""
    f = parse(text)
    out = [f"File  {f.origin_name} ({f.immediate_origin}) -> {f.destination_name} "
           f"({f.immediate_destination})  created {f.created:%Y-%m-%d %H:%M}  "
           f"modifier {f.file_id_modifier}"]
    for b in f.batches:
        sd = f" settles day {b.settlement_date}" if b.settlement_date else ""
        out.append(f"  Batch {b.batch_number}  {b.sec_code}  class {b.service_class}  "
                   f"{b.company_name} [{b.company_id}] '{b.entry_description}'  "
                   f"effective {b.effective_date}{sd}")
        for e in b.entries:
            kind = TRANSACTION_CODES.get(e.transaction_code, "?")
            out.append(f"    {e.trace_number}  {e.transaction_code} {kind:<30} "
                       f"{e.rdfi_routing} acct {e.account:<17} {money(e.amount):>14}  {e.name}")
            for a in e.addenda:
                c = a["addenda_type_code"]
                if c == "05":
                    out.append(f"        addenda 05: {a['payment_related_information']}")
                elif c == "99":
                    rc = a["return_reason_code"]
                    out.append(f"        RETURN {rc} {RETURN_REASONS.get(rc, '')} "
                               f"(original trace {a['original_entry_trace_number']})")
                elif c == "98":
                    cc = a["change_code"]
                    out.append(f"        NOC {cc} {CHANGE_CODES.get(cc, '')}: corrected "
                               f"'{a['corrected_data']}' (original trace "
                               f"{a['original_entry_trace_number']})")
    s = f.summary()
    out.append(f"  Totals: {s['entries']} entries, debits {money(s['total_debit_cents'])}, "
               f"credits {money(s['total_credit_cents'])}")
    return "\n".join(out)
