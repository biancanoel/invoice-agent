"""Billing-week math.

Timesheet weeks run Monday through Sunday. Invoice line items are those
weeks clipped to the billing month, so September 2026 becomes
September 1-6, 7-13, 14-20, 21-27, 28-30.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Month:
    year: int
    month: int

    @classmethod
    def parse(cls, text: str) -> Month:
        """Parse 'YYYY-MM' (e.g. '2026-09')."""
        try:
            year_str, month_str = text.split("-")
            month = cls(int(year_str), int(month_str))
        except ValueError:
            raise ValueError(f"Month must look like YYYY-MM, got {text!r}") from None
        if not 1 <= month.month <= 12:
            raise ValueError(f"Month out of range: {text!r}")
        return month

    @property
    def first_day(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def last_day(self) -> date:
        return date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])

    @property
    def invoice_number(self) -> str:
        """Invoice number is MM-YYYY, e.g. '09-2026'."""
        return f"{self.month:02d}-{self.year}"

    def contains(self, day: date) -> bool:
        return self.first_day <= day <= self.last_day


@dataclass(frozen=True)
class BillingWeek:
    """One Monday-Sunday timesheet week, clipped to the billing month."""

    week_start: date  # Monday of the full timesheet week (may be in the previous month)
    start: date  # first day inside the month
    end: date  # last day inside the month

    @property
    def label(self) -> str:
        """Invoice line label, e.g. 'September 1-6', or 'August 31' for a one-day week."""
        if self.start == self.end:
            return f"{self.start:%B} {self.start.day}"
        return f"{self.start:%B} {self.start.day}-{self.end.day}"


def week_start_for(day: date) -> date:
    """Monday of the Monday-Sunday week containing `day`."""
    return day - timedelta(days=day.weekday())


def billing_weeks(month: Month) -> list[BillingWeek]:
    """All Monday-Sunday weeks touching `month`, clipped to it, earliest first."""
    weeks = []
    monday = week_start_for(month.first_day)
    while monday <= month.last_day:
        sunday = monday + timedelta(days=6)
        weeks.append(
            BillingWeek(
                week_start=monday,
                start=max(monday, month.first_day),
                end=min(sunday, month.last_day),
            )
        )
        monday += timedelta(days=7)
    return weeks
