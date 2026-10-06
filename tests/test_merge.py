from datetime import date
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader

from invoice_agent.cli import main
from invoice_agent.merge import MergeError, Screenshot, merge_screenshots
from invoice_agent.weeks import Month

SEPT = Month.parse("2026-09")
SEPT_MONDAYS = [date(2026, 8, 31), date(2026, 9, 7), date(2026, 9, 14), date(2026, 9, 21), date(2026, 9, 28)]


def make_image(path: Path, size: tuple[int, int], mode: str = "RGB") -> Path:
    color = (200, 0, 0, 0) if mode == "RGBA" else (200, 0, 0)
    Image.new(mode, size, color).save(path)
    return path


def page_widths(pdf: Path) -> list[float]:
    return [float(page.mediabox.width) for page in PdfReader(pdf).pages]


@pytest.fixture
def sept_shots(tmp_path) -> list[Screenshot]:
    # Give each week a distinct width so page order is checkable in the PDF.
    # Files are named in reverse so filename order != week order.
    shots = []
    for i, monday in enumerate(SEPT_MONDAYS):
        path = make_image(tmp_path / f"Screenshot {9 - i}.png", (100 + 10 * i, 50))
        shots.append(Screenshot(path, monday))
    return shots


def test_pages_sorted_by_week_not_filename(tmp_path, sept_shots):
    result = merge_screenshots(list(reversed(sept_shots)), SEPT, tmp_path / "out.pdf")

    assert [s.week_start for s in result.included] == SEPT_MONDAYS
    assert len(page_widths(result.output)) == 5
    assert page_widths(result.output) == sorted(page_widths(result.output))
    assert result.missing_weeks == []


def test_transparent_png_is_flattened_onto_white(tmp_path):
    path = make_image(tmp_path / "clear.png", (40, 40), mode="RGBA")
    result = merge_screenshots([Screenshot(path, SEPT_MONDAYS[0])], SEPT, tmp_path / "out.pdf")
    assert len(PdfReader(result.output).pages) == 1


def test_weeks_outside_month_are_skipped(tmp_path, sept_shots):
    august = make_image(tmp_path / "aug.png", (60, 60))
    shots = [*sept_shots, Screenshot(august, date(2026, 8, 24))]

    result = merge_screenshots(shots, SEPT, tmp_path / "out.pdf")

    assert [s.path for s in result.skipped] == [august]
    assert len(result.included) == 5


def test_boundary_week_counts_for_both_months(tmp_path, sept_shots):
    boundary = sept_shots[0]  # week of Mon Aug 31 - Sun Sep 6
    aug = merge_screenshots([boundary], Month.parse("2026-08"), tmp_path / "aug.pdf")
    assert aug.included == [boundary]


def test_missing_weeks_are_reported(tmp_path, sept_shots):
    result = merge_screenshots(sept_shots[:2] + sept_shots[3:], SEPT, tmp_path / "out.pdf")
    assert result.missing_weeks == [date(2026, 9, 14)]


def test_duplicate_week_is_an_error(tmp_path, sept_shots):
    dup = Screenshot(make_image(tmp_path / "dup.png", (60, 60)), SEPT_MONDAYS[1])
    with pytest.raises(MergeError, match="Two screenshots"):
        merge_screenshots([*sept_shots, dup], SEPT, tmp_path / "out.pdf")


def test_non_monday_week_start_is_an_error(tmp_path, sept_shots):
    bad = Screenshot(sept_shots[0].path, date(2026, 9, 1))
    with pytest.raises(MergeError, match="not a Monday"):
        merge_screenshots([bad], SEPT, tmp_path / "out.pdf")


def test_no_screenshots_in_month_is_an_error(tmp_path):
    path = make_image(tmp_path / "old.png", (60, 60))
    with pytest.raises(MergeError, match="No screenshots"):
        merge_screenshots([Screenshot(path, date(2026, 1, 5))], SEPT, tmp_path / "out.pdf")


def test_cli_merge_with_weeks_flag(tmp_path, capsys):
    # Filenames sort a, b, c; the weeks are given out of order and as mid-week dates.
    for name in "abc":
        make_image(tmp_path / f"{name}.png", (60, 60))

    code = main(["merge", str(tmp_path), "--month", "2026-09", "--weeks", "2026-09-10,2026-09-01,2026-09-16"])

    out = capsys.readouterr().out
    assert code == 0
    assert (tmp_path / "Timesheets 09-2026.pdf").exists()
    assert out.index("b.png") < out.index("a.png") < out.index("c.png")
    assert "no screenshot for the week of Sep 21" in out


def test_cli_rejects_wrong_number_of_dates(tmp_path, capsys):
    make_image(tmp_path / "a.png", (60, 60))
    assert main(["merge", str(tmp_path), "--month", "2026-09", "--weeks", "2026-09-01,2026-09-08"]) == 1
