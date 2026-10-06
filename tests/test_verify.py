from datetime import date, timedelta
from pathlib import Path

import pytest
from PIL import Image

from invoice_agent import cli, timesheet
from invoice_agent.timesheet import TimesheetWeek
from invoice_agent.verify import verify_totals
from invoice_agent.weeks import Month

SEPT = Month.parse("2026-09")
SEPT_MONDAYS = [date(2026, 8, 31), date(2026, 9, 7), date(2026, 9, 14), date(2026, 9, 21), date(2026, 9, 28)]
H = 60  # minutes per hour, to keep expected values readable


def sheet(monday: date, daily_minutes: list[int], name: str = "", **kwargs) -> TimesheetWeek:
    days = {monday + timedelta(days=i): m for i, m in enumerate(daily_minutes)}
    return TimesheetWeek(
        image=Path(name or f"{monday}.png"),
        week_start=monday,
        day_minutes=days,
        total_minutes=sum(daily_minutes),
        confidence=kwargs.pop("confidence", "high"),
        **kwargs,
    )


def full_month() -> list[TimesheetWeek]:
    # Each week: 1h every weekday. The first week has Mon Aug 31 (outside Sept) and the
    # last week runs Mon Sep 28 - Sun Oct 4, so only Mon-Wed count.
    return [sheet(m, [H, H, H, H, H, 0, 0]) for m in SEPT_MONDAYS]


SEPT_TYPED = [5 * H] * 5  # every screenshot's weekly total


def test_all_match():
    result = verify_totals(SEPT, SEPT_TYPED, full_month())
    assert result.ok
    assert [c.screenshot_minutes for c in result.checks] == SEPT_TYPED


def test_boundary_weeks_invoice_only_days_in_month():
    # Week 1 includes Mon Aug 31; week 5 includes Thu Oct 1 - Sun Oct 4.
    result = verify_totals(SEPT, SEPT_TYPED, full_month())
    assert [c.invoice_minutes for c in result.checks] == [4 * H, 5 * H, 5 * H, 5 * H, 3 * H]
    assert result.invoice_minutes == 22 * H


def test_compares_exact_minutes():
    sheets = full_month()
    sheets[1] = sheet(SEPT_MONDAYS[1], [40, 0, 0, 0, 0, 0, 0])
    typed = [5 * H, 41, 5 * H, 5 * H, 5 * H]
    check = verify_totals(SEPT, typed, sheets).checks[1]
    assert check.status == "mismatch"
    assert check.detail == "You entered 41m, the screenshot's weekly total is 40m"


def test_entering_only_the_in_month_days_gets_a_hint():
    typed = [4 * H, 5 * H, 5 * H, 5 * H, 5 * H]
    check = verify_totals(SEPT, typed, full_month()).checks[0]
    assert check.status == "mismatch"
    assert check.detail == (
        "You entered 4h 00m, the screenshot's weekly total is 5h 00m "
        "(that's just the September days; enter the full weekly total)"
    )


def test_mismatched_week_still_reports_screenshot_values():
    check = verify_totals(SEPT, [H] * 5, full_month()).checks[4]
    assert (check.screenshot_minutes, check.invoice_minutes) == (5 * H, 3 * H)


def test_missing_screenshot():
    sheets = full_month()
    del sheets[2]
    check = verify_totals(SEPT, SEPT_TYPED, sheets).checks[2]
    assert check.status == "no_screenshot"
    assert "Sep 14" in check.detail


def test_duplicate_screenshot():
    sheets = [*full_month(), sheet(SEPT_MONDAYS[1], [H] * 5 + [0, 0], name="again.png")]
    assert verify_totals(SEPT, SEPT_TYPED, sheets).checks[1].status == "duplicate"


def test_unreadable_screenshot_is_not_trusted():
    sheets = full_month()
    sheets[3] = sheet(SEPT_MONDAYS[3], [H] * 5 + [0, 0], problems=["Daily totals add up to 5h but ..."])
    check = verify_totals(SEPT, SEPT_TYPED, sheets).checks[3]
    assert check.status == "unreadable"
    assert "add up" in check.detail


def test_low_confidence_is_unreadable():
    sheets = full_month()
    sheets[0] = sheet(SEPT_MONDAYS[0], [H] * 5 + [0, 0], confidence="low", notes="Total row cut off")
    check = verify_totals(SEPT, SEPT_TYPED, sheets).checks[0]
    assert check.status == "unreadable"
    assert "Total row cut off" in check.detail


def test_screenshots_outside_month_are_ignored():
    extra = sheet(date(2026, 8, 24), [H] * 7)
    result = verify_totals(SEPT, SEPT_TYPED, [*full_month(), extra])
    assert result.ok
    assert result.ignored == [extra]


def test_wrong_number_of_entries():
    with pytest.raises(ValueError, match="5 invoice lines"):
        verify_totals(SEPT, SEPT_TYPED[:3], full_month())


@pytest.fixture
def five_images(tmp_path, monkeypatch):
    for i in range(5):
        Image.new("RGB", (20, 20), "white").save(tmp_path / f"{i}.png")
    sheets = iter(full_month())
    monkeypatch.setattr(cli, "make_client", lambda: None)
    monkeypatch.setattr(timesheet, "read_timesheet", lambda path, client, refresh=False: next(sheets))
    return tmp_path


def test_cli_verify_accepts_screenshot_style_entries(five_images, capsys):
    code = cli.main(["verify", str(five_images), "--month", "2026-09", "--hours", "5h 00m, 5h, 5h 00m, 6h, 5h"])

    out = capsys.readouterr().out
    assert code == 1
    assert "September 21-27" in out and "MISMATCH" in out
    assert "You entered 6h 00m, the screenshot's weekly total is 5h 00m" in out
    assert "22h 00m" in out and "22" in out  # invoice total uses only September days


def test_cli_verify_all_match(five_images, capsys):
    assert cli.main(["verify", str(five_images), "--month", "2026-09", "--hours", "5h,5h,5h,5h,5h"]) == 0
    assert "All weeks match." in capsys.readouterr().out


def test_cli_verify_rejects_unreadable_entry(five_images, capsys):
    assert cli.main(["verify", str(five_images), "--month", "2026-09", "--hours", "4h,5h,five,5h,3h"]) == 1
    assert "Can't read 'five'" in capsys.readouterr().err
