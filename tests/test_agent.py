import asyncio
from datetime import date, timedelta

import pytest
from PIL import Image

from invoice_agent import agent, timesheet
from invoice_agent.agent import AUTO_APPROVED, CREATE_INVOICE, InvoiceTools, ToolInputError
from invoice_agent.config import Config
from invoice_agent.timesheet import TimesheetWeek
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ResultMessage

H = 60
SEPT_MONDAYS = [date(2026, 8, 31), date(2026, 9, 7), date(2026, 9, 14), date(2026, 9, 21), date(2026, 9, 28)]
DAILY = [[H, 40, 0, 0, 0, 0, 0], [2 * H] * 5 + [0, 0], [90] * 6 + [0], [0] * 7, [H, H, 20, 5 * H, 0, 0, 0]]
TOTALS = ["1h 40m", "10h", "9h", "0m", "7h 20m"]  # each screenshot's weekly total
ARGS = {"folder": "data/2026-09", "month": "2026-09", "weekly_totals": TOTALS}


class FakeWorkspace:
    def __init__(self):
        self.copies = []
        self.cells = {}

    def find_file(self, name, folder_id):
        return None

    def copy_file(self, file_id, name, folder_id):
        self.copies.append(name)
        return "NEW_ID"

    def write_cells(self, spreadsheet_id, sheet, cells):
        self.cells = dict(cells)

    def export_pdf(self, spreadsheet_id, sheet):
        return b"%PDF-fake"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project folder with data/2026-09 holding five screenshots read as SEPT weeks."""
    folder = tmp_path / "data" / "2026-09"
    folder.mkdir(parents=True)
    sheets = {}
    for i, (monday, daily) in enumerate(zip(SEPT_MONDAYS, DAILY)):
        path = folder / f"Screenshot {i}.png"
        Image.new("RGB", (20, 20), "white").save(path)
        days = {monday + timedelta(days=d): m for d, m in enumerate(daily)}
        sheets[path.name] = TimesheetWeek(path, monday, days, sum(daily), "high")
    monkeypatch.setattr(timesheet, "read_timesheet", lambda path, client, refresh=False: sheets[path.name])
    return tmp_path


def make_tools(project, answer="y", workspace=None):
    workspace = workspace or FakeWorkspace()
    tools = InvoiceTools(
        Config(template_id="T", folder_id="F"),
        project / "data",
        workspace_factory=lambda config: workspace,
        ask=lambda prompt: answer,
        client_factory=lambda: None,
    )
    return tools, workspace


def approve(tools, args, tool_name=CREATE_INVOICE):
    return asyncio.run(tools.approve_tool(tool_name, args, context=None))


# --- folders ------------------------------------------------------------------


@pytest.mark.parametrize("text", ["data/2026-09", "2026-09"])
def test_folder_inside_data(project, text):
    tools, _ = make_tools(project)
    assert tools.folder(text) == (project / "data" / "2026-09").resolve()


@pytest.mark.parametrize("text", ["../secrets", "data/../..", "/tmp"])
def test_folder_outside_data_is_rejected(project, text):
    tools, _ = make_tools(project)
    with pytest.raises(ToolInputError, match="outside data/"):
        tools.folder(text)


def test_missing_folder(project):
    tools, _ = make_tools(project)
    with pytest.raises(ToolInputError, match="No folder"):
        tools.folder("data/2026-10")


# --- read/verify/merge/preview ------------------------------------------------


def test_verify_hours_reports_table(project):
    tools, _ = make_tools(project)
    text = tools.verify_hours(ARGS)
    assert "All weeks match." in text
    assert "September 28-30" in text


def test_verify_hours_bad_entry_is_a_tool_error(project):
    tools, _ = make_tools(project)
    with pytest.raises(ToolInputError, match="Can't read 'ten'"):
        tools.verify_hours({**ARGS, "weekly_totals": ["ten", "10h", "9h", "0m", "7h 20m"]})


def test_merge_timesheets_writes_pdf(project):
    tools, _ = make_tools(project)
    text = tools.merge_timesheets({"folder": "data/2026-09", "month": "2026-09"})
    assert "(5 pages)" in text
    assert (project / "data" / "2026-09" / "Timesheets 09-2026.pdf").exists()


def test_preview_creates_nothing(project):
    tools, workspace = make_tools(project)
    text = tools.preview_invoice(ARGS)
    assert "B20   September 1-6" in text and "Preview only" in text
    assert workspace.copies == []


def test_preview_refuses_mismatched_hours(project):
    tools, _ = make_tools(project)
    with pytest.raises(ToolInputError, match="MISMATCH"):
        tools.preview_invoice({**ARGS, "weekly_totals": ["1h", "10h", "9h", "0m", "7h 20m"]})


# --- the approval gate ----------------------------------------------------------


def test_create_without_approval_is_refused(project):
    tools, workspace = make_tools(project)
    with pytest.raises(ToolInputError, match="Not approved"):
        tools.create_invoice(ARGS)
    assert workspace.copies == []


def test_user_approves_then_invoice_is_created_once(project):
    tools, workspace = make_tools(project, answer="y")

    assert isinstance(approve(tools, ARGS), PermissionResultAllow)
    assert "Created https://docs.google.com/spreadsheets/d/NEW_ID/edit" in tools.create_invoice(ARGS)
    assert workspace.copies == ["Invoice September 2026"]

    with pytest.raises(ToolInputError, match="Not approved"):  # approval is single-use
        tools.create_invoice(ARGS)


def test_user_declines(project):
    tools, workspace = make_tools(project, answer="n")
    result = approve(tools, ARGS)
    assert isinstance(result, PermissionResultDeny) and "declined" in result.message
    with pytest.raises(ToolInputError, match="Not approved"):
        tools.create_invoice(ARGS)


def test_approval_covers_only_the_exact_request(project):
    tools, workspace = make_tools(project, answer="y")
    approve(tools, ARGS)
    changed = {**ARGS, "weekly_totals": ["1h 40m", "10h", "9h", "0m", "7h 21m"]}
    with pytest.raises(ToolInputError, match="Not approved"):
        tools.create_invoice(changed)


def test_creates_exactly_what_was_approved(project, monkeypatch):
    tools, workspace = make_tools(project, answer="y")
    approve(tools, ARGS)

    # A screenshot reading changes after approval; the approved plan is still what gets written.
    monkeypatch.setattr(timesheet, "read_timesheet", lambda *a, **k: pytest.fail("re-read after approval"))
    tools.create_invoice(ARGS)

    assert workspace.cells["E20"] == 0.67  # September 1-6 as approved


def test_unverified_request_is_denied_without_asking(project):
    tools, _ = make_tools(project)
    tools.ask = lambda prompt: pytest.fail("asked to approve unverified hours")
    result = approve(tools, {**ARGS, "weekly_totals": ["1h", "10h", "9h", "0m", "7h 20m"]})
    assert isinstance(result, PermissionResultDeny) and "MISMATCH" in result.message


def test_other_tools_reaching_the_callback_are_denied(project):
    tools, _ = make_tools(project)
    tools.ask = lambda prompt: pytest.fail("asked about another tool")
    assert isinstance(approve(tools, {"command": "ls"}, tool_name="Bash"), PermissionResultDeny)


# --- SDK wiring -----------------------------------------------------------------


def test_options_lock_down_the_agent(project):
    tools, _ = make_tools(project)
    options = tools.options("claude-sonnet-5-5", project)
    assert options.tools == []
    assert CREATE_INVOICE not in options.allowed_tools
    assert set(options.allowed_tools) == set(AUTO_APPROVED)
    assert options.permission_mode == "default"
    assert options.setting_sources == []
    assert options.can_use_tool == tools.approve_tool


def test_tool_handler_returns_errors_to_claude(project):
    tools, _ = make_tools(project)
    handlers = {t.name: t.handler for t in tools.sdk_tools()}
    assert set(handlers) == {n.removeprefix("mcp__invoice__") for n in [*AUTO_APPROVED, CREATE_INVOICE]}

    ok = asyncio.run(handlers["verify_hours"](ARGS))
    assert "is_error" not in ok and "All weeks match." in ok["content"][0]["text"]

    refused = asyncio.run(handlers["create_invoice"](ARGS))
    assert refused["is_error"] is True and "Not approved" in refused["content"][0]["text"]


# --- system prompt ----------------------------------------------------------------


def test_system_prompt_fills_in_the_date():
    from invoice_agent.agent import system_prompt

    text = system_prompt(date(2026, 10, 6))
    assert "Today is Tuesday, October 6, 2026." in text
    assert "{today}" not in text


def test_system_prompt_names_every_tool_that_exists(project):
    """Keeps prompts/system.md in sync with the tools if either is renamed."""
    import re

    from invoice_agent.agent import system_prompt

    tools, _ = make_tools(project)
    names = {t.name for t in tools.sdk_tools()}
    mentioned = set(re.findall(r"\b(?:read|verify|merge|preview|create)_[a-z_]+\b", system_prompt(date.today())))
    assert mentioned <= names, f"prompt mentions tools that don't exist: {mentioned - names}"
    assert {"verify_hours", "merge_timesheets", "preview_invoice", "create_invoice"} <= mentioned


# --- chat -----------------------------------------------------------------------


class FakeSDKClient:
    """Reports a running session total on each turn, as the real SDK does in streaming mode."""

    def __init__(self, options):
        self.turns = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, message):
        self.turns += 1

    async def receive_response(self):
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
            session_id="s", total_cost_usd=0.01 * self.turns,
        )


def test_chat_reports_the_running_total_not_a_sum(project, monkeypatch, capsys):
    tools, _ = make_tools(project)
    monkeypatch.setattr(agent, "ClaudeSDKClient", FakeSDKClient)
    replies = iter(["second", "third", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(replies))

    asyncio.run(agent.chat(tools, "model", project, first_message="first"))

    assert "Session cost: $0.03" in capsys.readouterr().out  # summing would say $0.06
