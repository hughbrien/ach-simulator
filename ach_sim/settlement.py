"""Settlement calendar: Fed business days and FedACH-style processing windows.

Window deadlines are the FedACH Same Day submission deadlines (Eastern Time).
The simulator uses them to label which window a file landed in; it does not
model every operator schedule.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Federal Reserve holidays. A holiday falling on Sunday is observed Monday;
# one falling on Saturday is NOT observed on the Friday before.
FED_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 9, 7), date(2026, 10, 12), date(2026, 11, 11),
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 5, 31),
    date(2027, 7, 5), date(2027, 9, 6), date(2027, 10, 11), date(2027, 11, 11),
    date(2027, 11, 25),
}

SAME_DAY_WINDOWS = [  # (label, submission deadline ET)
    ("SDA-1", time(10, 30)),
    ("SDA-2", time(14, 45)),
    ("SDA-3", time(16, 45)),
]


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in FED_HOLIDAYS


def next_business_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def current_or_next_business_day(d: date) -> date:
    return d if is_business_day(d) else next_business_day(d)


def settlement_for(effective: date, submitted: datetime, same_day_eligible: bool):
    """Return (settlement_date, window_label) for a batch.

    - Effective date today (business day), submitted before the last Same Day
      deadline, and eligible -> settles today in the first open Same Day window.
    - Effective date in the future -> settles on that date (or next business day).
    - Effective date today/past but not Same Day -> next business day.
    """
    local = submitted.astimezone(ET)
    today = local.date()
    if effective > today:
        return current_or_next_business_day(effective), "FUTURE-DATED"
    if is_business_day(today) and same_day_eligible and effective <= today:
        for label, deadline in SAME_DAY_WINDOWS:
            if local.time() <= deadline:
                return today, label
    return next_business_day(today), "NEXT-DAY"
