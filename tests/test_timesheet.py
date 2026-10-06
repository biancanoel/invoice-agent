from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from invoice_agent import cli
from invoice_agent.timesheet import (
    DayCell,
    Extraction,
    TimesheetReadError,
    read_timesheet,
    sidecar_path,
    to_week,
)


def extraction(monday: date, totals: list[str], weekly_total: str, confidence="high") -> Extraction:
    days = [DayCell(date=str(monday + timedelta(days=i)), total=t) for i, t in enumerate(totals)]
    return Extraction(
        date_range_header="", days=days, weekly_total=weekly_total, confidence=confidence, notes=""
    )


# The week of Mon Aug 31 - Sun Sep 6, 2026 from the example screenshot.
BOUNDARY = extraction(date(2026, 8, 31), ["1h 30m", "1h 00m", "0m", "0m", "0m", "0m", "0m"], "2h 30m")


class FakeClient:
    """Stands in for anthropic.Anthropic; returns canned extractions in order."""

    def __init__(self, *results, stop_reason="end_turn"):
        self.results = list(results)
        self.calls = 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))
        self.stop_reason = stop_reason

    def _parse(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return SimpleNamespace(stop_reason=self.stop_reason, parsed_output=self.results.pop(0))


@pytest.fixture
def image(tmp_path) -> Path:
    path = tmp_path / "Screenshot 2026-10-01 at 1.48.39 PM.png"
    Image.new("RGB", (20, 20), "white").save(path)
    return path


def test_boundary_week_splits_by_month(image):
    week = to_week(image, BOUNDARY)

    assert week.ok
    assert week.week_start == date(2026, 8, 31)
    assert week.total_minutes == 150
    assert week.minutes_in(date(2026, 8, 1), date(2026, 8, 31)) == 90  # Aug 31 only
    assert week.minutes_in(date(2026, 9, 1), date(2026, 9, 30)) == 60  # Sep 1-6


def test_daily_totals_that_dont_add_up_are_flagged(image):
    bad = extraction(date(2026, 8, 31), ["1h 30m", "1h 00m", "0m", "0m", "0m", "0m", "0m"], "3h 00m")
    week = to_week(image, bad)
    assert not week.ok
    assert "add up to 2h 30m" in week.problems[0]


def test_week_not_starting_monday_is_flagged(image):
    week = to_week(image, extraction(date(2026, 9, 1), ["0m"] * 7, "0m"))
    assert any("not a Monday" in p for p in week.problems)


def test_missing_day_is_flagged(image):
    six_days = extraction(date(2026, 8, 31), ["0m"] * 6, "0m")
    assert any("7 consecutive days" in p for p in to_week(image, six_days).problems)


def test_low_confidence_is_not_ok(image):
    assert not to_week(image, extraction(date(2026, 8, 31), ["0m"] * 7, "0m", confidence="low")).ok


def test_result_is_cached_and_reused(image):
    client = FakeClient(BOUNDARY)

    first = read_timesheet(image, client)
    second = read_timesheet(image, client)

    assert client.calls == 1
    assert sidecar_path(image).exists()
    assert first.day_minutes == second.day_minutes


def test_changed_image_is_read_again(image):
    client = FakeClient(BOUNDARY, BOUNDARY)
    read_timesheet(image, client)
    Image.new("RGB", (30, 30), "white").save(image)
    read_timesheet(image, client)
    assert client.calls == 2


def test_refresh_ignores_cache(image):
    client = FakeClient(BOUNDARY, BOUNDARY)
    read_timesheet(image, client)
    read_timesheet(image, client, refresh=True)
    assert client.calls == 2


@pytest.mark.parametrize("damaged", ["{not json", "[]", '{"sha256": "x"}', '{"extraction": {}}'])
def test_damaged_cache_is_ignored_and_reread(image, damaged):
    sidecar_path(image).write_text(damaged)
    client = FakeClient(BOUNDARY)
    assert read_timesheet(image, client).week_start == date(2026, 8, 31)
    assert client.calls == 1


def test_cache_with_wrong_shape_for_this_image_is_reread(image):
    import hashlib, json

    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    sidecar_path(image).write_text(json.dumps({"sha256": digest, "extraction": {"days": "oops"}}))
    client = FakeClient(BOUNDARY)
    read_timesheet(image, client)
    assert client.calls == 1


def test_failed_checks_are_not_cached(image):
    bad = extraction(date(2026, 8, 31), ["1h 00m"] + ["0m"] * 6, "9h 00m")
    read_timesheet(image, FakeClient(bad))
    assert not sidecar_path(image).exists()


def test_request_sends_the_image(image):
    client = FakeClient(BOUNDARY)
    read_timesheet(image, client)
    content = client.last_kwargs["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert client.last_kwargs["output_format"] is Extraction


def test_refusal_raises(image):
    with pytest.raises(TimesheetReadError, match="declined"):
        read_timesheet(image, FakeClient(BOUNDARY, stop_reason="refusal"))


def test_cli_merge_orders_pages_from_screenshot_dates(tmp_path, monkeypatch, capsys):
    # Capture-time filenames are in the opposite order from the weeks they show.
    later_week = extraction(date(2026, 9, 7), ["1h 00m"] + ["0m"] * 6, "1h 00m")
    for name in ("Screenshot A.png", "Screenshot B.png"):
        Image.new("RGB", (20, 20), "white").save(tmp_path / name)
    monkeypatch.setattr(cli, "make_client", lambda: FakeClient(later_week, BOUNDARY))

    code = cli.main(["merge", str(tmp_path), "--month", "2026-09"])

    out = capsys.readouterr().out
    assert code == 0
    assert out.index("week of Aug 31: Screenshot B.png") < out.index("week of Sep 07: Screenshot A.png")


def test_cli_read_shows_in_month_hours(tmp_path, monkeypatch, capsys):
    Image.new("RGB", (20, 20), "white").save(tmp_path / "s.png")
    monkeypatch.setattr(cli, "make_client", lambda: FakeClient(BOUNDARY))

    assert cli.main(["read", str(tmp_path), "--month", "2026-09"]) == 0
    assert "2h 30m total, 1h 00m in September" in capsys.readouterr().out


def test_workspace_id_header_from_env(monkeypatch):
    from invoice_agent.timesheet import make_client

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_123")
    assert make_client().default_headers["anthropic-workspace-id"] == "wrkspc_123"

    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID")
    assert "anthropic-workspace-id" not in make_client().default_headers
