from datetime import date, timedelta
from pathlib import Path

import pytest
from PIL import Image

from invoice_agent import cli, timesheet
from invoice_agent.config import Config, Layout, load_config
from invoice_agent.invoice import InvoiceError, create_invoice, plan_invoice, sheets_date
from invoice_agent.timesheet import TimesheetWeek
from invoice_agent.verify import verify_totals
from invoice_agent.weeks import Month

SEPT = Month.parse("2026-09")
SEPT_MONDAYS = [date(2026, 8, 31), date(2026, 9, 7), date(2026, 9, 14), date(2026, 9, 21), date(2026, 9, 28)]
H = 60
NAME_FORMAT = "Invoice {month_name} {year}"
CONFIG = Config(template_id="TEMPLATE", folder_id="FOLDER")


def sheet(monday: date, daily_minutes: list[int]) -> TimesheetWeek:
    days = {monday + timedelta(days=i): m for i, m in enumerate(daily_minutes)}
    return TimesheetWeek(Path(f"{monday}.png"), monday, days, sum(daily_minutes), "high")


def sept_sheets() -> list[TimesheetWeek]:
    # Week 1: 1h on Mon Aug 31, then 40m on Tue Sep 1. Week 5: Sep 28-30 plus Oct days.
    return [
        sheet(SEPT_MONDAYS[0], [H, 40, 0, 0, 0, 0, 0]),
        sheet(SEPT_MONDAYS[1], [2 * H] * 5 + [0, 0]),
        sheet(SEPT_MONDAYS[2], [90] * 6 + [0]),
        sheet(SEPT_MONDAYS[3], [0] * 7),
        sheet(SEPT_MONDAYS[4], [H, H, 20, 5 * H, 0, 0, 0]),
    ]


SEPT_TYPED = [100, 10 * H, 9 * H, 0, 440]  # each screenshot's weekly total


def verified():
    return verify_totals(SEPT, SEPT_TYPED, sept_sheets())


class FakeWorkspace:
    def __init__(self, existing: str | None = None, fail_on_write: bool = False):
        self.existing = existing
        self.fail_on_write = fail_on_write
        self.copies: list[tuple[str, str, str]] = []
        self.writes: list[tuple[str, str, list]] = []

    def find_file(self, name, folder_id):
        return self.existing

    def copy_file(self, file_id, name, folder_id):
        self.copies.append((file_id, name, folder_id))
        return "NEW_ID"

    def write_cells(self, spreadsheet_id, sheet, cells):
        if self.fail_on_write:
            raise RuntimeError("quota exceeded")
        self.writes.append((spreadsheet_id, sheet, cells))

    def export_pdf(self, spreadsheet_id, sheet):
        return b"%PDF-fake"


def test_sheets_date_serial():
    assert sheets_date(date(2026, 9, 30)) == 46295  # the value in the September template


def test_plan_uses_only_in_month_time():
    plan = plan_invoice(verified(), NAME_FORMAT)

    assert plan.name == "Invoice September 2026"
    assert plan.invoice_number == "09-2026"
    assert plan.submitted_on == plan.due_date == date(2026, 9, 30)
    assert [(line.description, line.hours) for line in plan.lines] == [
        ("September 1-6", 0.67),  # 40m; Aug 31's hour is excluded
        ("September 7-13", 10),
        ("September 14-20", 9),
        ("September 21-27", 0),
        ("September 28-30", 2.33),  # 2h 20m; the Oct 1 hours are excluded
    ]
    assert plan.total_hours == 22


def test_plan_refuses_unverified_hours():
    bad = verify_totals(SEPT, [H] * 5, sept_sheets())
    with pytest.raises(InvoiceError, match="verified"):
        plan_invoice(bad, NAME_FORMAT)


def test_cells_fill_used_rows_and_clear_the_rest():
    cells = dict(plan_invoice(verified(), NAME_FORMAT).cells(Layout()))

    assert cells["B9"] == "Submitted on 9/30/2026"
    assert cells["F12"] == "09-2026"
    assert cells["F16"] == 46295
    assert (cells["B20"], cells["E20"]) == ("September 1-6", 0.67)
    assert (cells["B24"], cells["E24"]) == ("September 28-30", 2.33)
    assert (cells["B25"], cells["E25"]) == ("", 0)  # the sixth row is unused in September
    assert not any(cell.endswith(("26", "27", "28")) for cell in cells)  # Notes/Subtotal/Total untouched
    assert not any(cell.startswith(("F2", "G")) for cell in cells)  # rates and formulas untouched


def test_too_many_lines_for_template():
    plan = plan_invoice(verified(), NAME_FORMAT)
    with pytest.raises(ValueError, match="only has 4 rows"):
        plan.cells(Layout(line_rows=4))


def test_create_copies_template_writes_cells_and_saves_pdf(tmp_path):
    workspace = FakeWorkspace()
    plan = plan_invoice(verified(), NAME_FORMAT)

    created = create_invoice(plan, CONFIG, workspace, tmp_path / "out" / "Invoice 09-2026.pdf")

    assert workspace.copies == [("TEMPLATE", "Invoice September 2026", "FOLDER")]
    assert workspace.writes[0][:2] == ("NEW_ID", "Invoice")
    assert created.url == "https://docs.google.com/spreadsheets/d/NEW_ID/edit"
    assert created.pdf.read_bytes() == b"%PDF-fake"


def test_create_refuses_to_overwrite_existing_invoice(tmp_path):
    workspace = FakeWorkspace(existing="OLD_ID")
    with pytest.raises(InvoiceError, match="already exists.*OLD_ID"):
        create_invoice(plan_invoice(verified(), NAME_FORMAT), CONFIG, workspace, tmp_path / "x.pdf")
    assert workspace.copies == []


def test_failure_after_copy_reports_the_new_sheet(tmp_path):
    workspace = FakeWorkspace(fail_on_write=True)
    with pytest.raises(InvoiceError, match="Created .*NEW_ID.* quota exceeded"):
        create_invoice(plan_invoice(verified(), NAME_FORMAT), CONFIG, workspace, tmp_path / "x.pdf")
    assert not (tmp_path / "x.pdf").exists()


def test_load_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('template_id = "T"\nfolder_id = "F"\n[layout]\nline_rows = 12\n')
    config = load_config(path)
    assert (config.template_id, config.folder_id, config.layout.line_rows) == ("T", "F", 12)
    assert config.layout.hours_column == "E"


def test_load_config_rejects_typos(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('template_id = "T"\nfolder_id = "F"\ntemplate = "oops"\n')
    with pytest.raises(ValueError, match="template"):
        load_config(path)


def test_example_config_loads():
    assert load_config(Path(__file__).parent.parent / "config.example.toml").layout == Layout()


# --- CLI ---------------------------------------------------------------------

HOURS_ARG = "1h 40m, 10h, 9h, 0m, 7h 20m"


@pytest.fixture
def sept_folder(tmp_path, monkeypatch):
    folder = tmp_path / "2026-09"
    folder.mkdir()
    for i in range(5):
        Image.new("RGB", (20, 20), "white").save(folder / f"{i}.png")
    sheets = iter(sept_sheets())
    monkeypatch.setattr(cli, "make_client", lambda: None)
    monkeypatch.setattr(timesheet, "read_timesheet", lambda path, client, refresh=False: next(sheets))
    return folder


def run_invoice(folder, *extra, config_path):
    return cli.main(["invoice", str(folder), "--month", "2026-09", "--hours", HOURS_ARG,
                     "--config", str(config_path), *extra])


def test_cli_dry_run_works_without_config(sept_folder, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_google_workspace", lambda config: pytest.fail("dry run touched Google"))

    assert run_invoice(sept_folder, "--dry-run", config_path=tmp_path / "missing.toml") == 0

    out = capsys.readouterr().out
    assert "Invoice September 2026" in out
    assert "B20   September 1-6" in out
    assert "Dry run: nothing was created." in out


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('template_id = "TEMPLATE"\nfolder_id = "FOLDER"\n')
    return path


def test_cli_declining_creates_nothing(sept_folder, config_path, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    monkeypatch.setattr(cli, "_google_workspace", lambda config: pytest.fail("touched Google without approval"))

    assert run_invoice(sept_folder, config_path=config_path) == 1
    assert "Cancelled" in capsys.readouterr().out


def test_cli_confirming_creates_invoice(sept_folder, config_path, monkeypatch, capsys):
    workspace = FakeWorkspace()
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    monkeypatch.setattr(cli, "_google_workspace", lambda config: workspace)

    assert run_invoice(sept_folder, config_path=config_path) == 0
    assert (sept_folder / "Invoice 09-2026.pdf").read_bytes() == b"%PDF-fake"
    assert "Created https://docs.google.com/spreadsheets/d/NEW_ID/edit" in capsys.readouterr().out


def test_cli_mismatch_stops_before_planning(sept_folder, config_path, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked to confirm unverified hours"))
    code = cli.main(["invoice", str(sept_folder), "--month", "2026-09", "--hours", "1h,10h,9h,0m,7h 20m",
                     "--config", str(config_path)])
    assert code == 1
    assert "MISMATCH" in capsys.readouterr().out
