from datetime import date

import pytest

from invoice_agent.weeks import Month, billing_weeks, week_start_for


def labels(month: str) -> list[str]:
    return [w.label for w in billing_weeks(Month.parse(month))]


def test_september_2026_starts_midweek():
    # Sep 1, 2026 is a Tuesday; the first timesheet week starts Mon Aug 31.
    assert labels("2026-09") == ["September 1-6", "September 7-13", "September 14-20", "September 21-27", "September 28-30"]
    assert billing_weeks(Month.parse("2026-09"))[0].week_start == date(2026, 8, 31)


def test_august_2026_ends_with_single_day_week():
    # Aug 31, 2026 is a Monday, so the last line item is a single day.
    assert labels("2026-08")[-1] == "August 31"
    assert billing_weeks(Month.parse("2026-08"))[-1].week_start == date(2026, 8, 31)


def test_month_starting_on_monday():
    # June 2026 starts on a Monday.
    assert labels("2026-06")[0] == "June 1-7"


def test_six_week_month():
    # Mar 2026 starts on a Sunday and has 31 days: Mar 1, 2-8, ..., 30-31.
    assert labels("2026-03") == [
        "March 1", "March 2-8", "March 9-15", "March 16-22", "March 23-29", "March 30-31",
    ]


def test_invoice_number_and_file_names():
    month = Month.parse("2026-09")
    assert month.invoice_number == "09-2026"
    assert month.invoice_pdf_name == "Invoice 09-2026.pdf"
    assert month.timesheets_pdf_name == "Timesheets 09-2026.pdf"


def test_week_start_for_sunday_goes_back_to_monday():
    assert week_start_for(date(2026, 9, 6)) == date(2026, 8, 31)


@pytest.mark.parametrize("bad", ["2026-13", "Sept 2026", "2026"])
def test_bad_month(bad):
    with pytest.raises(ValueError):
        Month.parse(bad)
