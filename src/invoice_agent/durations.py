"""Converting between time text, minutes, and invoice hours.

Everything inside the program is whole minutes. Text like "1h 30m" (the
timesheet app's style) comes in through parse_duration / parse_entry and goes
out through format_duration; to_hours gives the decimal hours for the invoice.
"""

from __future__ import annotations

import re

_DURATION = re.compile(r"^(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?$")
_EMPTY = {"", "-", "—", "–"}  # how the timesheet app shows a day with no time


def parse_duration(text: str) -> int:
    """Timesheet text to minutes: '1h 30m' -> 90, '45m' -> 45, '2h' -> 120, '—' -> 0."""
    text = text.strip().lower()
    if text in _EMPTY:
        return 0
    match = _DURATION.match(text)
    if not match or not any(match.groups()):
        raise ValueError(f"Can't parse duration {text!r}")
    hours, minutes = match.groups()
    return int(hours or 0) * 60 + int(minutes or 0)


def parse_entry(text: str) -> int:
    """What you typed to minutes: '40m', '1h 30m', '2h', or decimal hours like '1.5'.

    Stricter than parse_duration: a blank entry is an error, not zero.
    """
    if not text.strip():
        raise ValueError("Empty entry; type 0m for a week with no time")
    try:
        return parse_duration(text)
    except ValueError:
        pass
    try:
        return round(float(text) * 60)
    except ValueError:
        raise ValueError(f"Can't read {text.strip()!r} as time; use something like 40m, 1h 30m, or 1.5") from None


def format_duration(minutes: int) -> str:
    """Minutes to timesheet text: 90 -> '1h 30m', 40 -> '40m'."""
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins:02d}m" if hours else f"{mins}m"


def to_hours(minutes: int) -> float:
    """Decimal hours as they appear on the invoice: 40 -> 0.67."""
    return round(minutes / 60, 2)
