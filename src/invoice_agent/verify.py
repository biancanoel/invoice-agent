"""verify_totals: compare typed weekly time against what the screenshots show.

Pure Python. The screenshots have already been read by read_timesheet; this
only does the comparison, so it never calls the API.

For every week you type the screenshot's weekly total (bottom-right), exactly
as shown, e.g. "15h 20m". That's checked against the screenshot in whole
minutes. The invoice amount for weeks that cross into another month is then
worked out from the screenshot's daily totals, so you never add anything up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from invoice_agent.durations import format_duration
from invoice_agent.timesheet import TimesheetWeek
from invoice_agent.weeks import BillingWeek, Month, billing_weeks

Status = Literal["match", "mismatch", "no_screenshot", "unreadable", "duplicate"]


@dataclass(frozen=True)
class WeekCheck:
    week: BillingWeek
    typed_minutes: int  # what you typed: the full Monday-Sunday week
    screenshot_minutes: int | None  # the screenshot's weekly total
    invoice_minutes: int | None  # days inside the month only; this goes on the invoice
    status: Status
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "match"


@dataclass(frozen=True)
class VerifyResult:
    month: Month
    checks: list[WeekCheck]
    ignored: list[TimesheetWeek]  # screenshots for weeks outside the month

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def invoice_minutes(self) -> int:
        return sum(c.invoice_minutes or 0 for c in self.checks)


def verify_totals(month: Month, typed: list[int], timesheets: list[TimesheetWeek]) -> VerifyResult:
    """Check each invoice line's typed minutes against its screenshot.

    `typed` has one entry per invoice line, earliest week first: each week's
    full Monday-Sunday total, as shown on its screenshot.
    """
    weeks = billing_weeks(month)
    if len(typed) != len(weeks):
        labels = ", ".join(w.label for w in weeks)
        raise ValueError(f"{month.invoice_number} has {len(weeks)} invoice lines ({labels}); got {len(typed)} entries")

    by_start: dict[date, list[TimesheetWeek]] = {}
    for sheet in timesheets:
        by_start.setdefault(sheet.week_start, []).append(sheet)
    in_month = {w.week_start for w in weeks}
    ignored = [s for s in timesheets if s.week_start not in in_month]

    checks = [_check(week, minutes, by_start.get(week.week_start, [])) for week, minutes in zip(weeks, typed)]
    return VerifyResult(month=month, checks=checks, ignored=ignored)


def _check(week: BillingWeek, typed: int, sheets: list[TimesheetWeek]) -> WeekCheck:
    if not sheets:
        return WeekCheck(week, typed, None, None, "no_screenshot", f"No screenshot for the week of {week.week_start:%b %d}")
    if len(sheets) > 1:
        names = ", ".join(s.image.name for s in sheets)
        return WeekCheck(week, typed, None, None, "duplicate", f"More than one screenshot for this week: {names}")

    sheet = sheets[0]
    total = sheet.total_minutes
    in_month = sheet.minutes_in(week.start, week.end)
    if not sheet.ok:
        reasons = "; ".join(sheet.problems) or f"{sheet.confidence} confidence: {sheet.notes}"
        return WeekCheck(week, typed, total, in_month, "unreadable", f"{sheet.image.name}: {reasons}")
    if typed != total:
        detail = f"You entered {format_duration(typed)}, the screenshot's weekly total is {format_duration(total)}"
        if typed == in_month:
            detail += f" (that's just the {week.start:%B} days; enter the full weekly total)"
        return WeekCheck(week, typed, total, in_month, "mismatch", detail)
    return WeekCheck(week, typed, total, in_month, "match")
