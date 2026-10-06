"""Phase 2: a conversational agent over the Phase 1 tools, using the Claude Agent SDK.

You describe the month in plain language; Claude decides which tools to call.
The SDK runs the agent loop. This module only defines the tools, the rules
for which ones run without asking, and a small terminal chat.

Safety:
- Claude Code's built-in tools (shell, file editing, web) are turned off;
  the agent can only call the invoice tools below.
- Tools only touch folders inside data/.
- create_invoice is never auto-approved. Every call goes to `approve_tool`,
  which shows the plan and asks you y/N in the terminal. The tool itself also
  refuses to run unless that exact request was approved, so a configuration
  mistake can't skip the prompt.
"""

from __future__ import annotations

import asyncio
import json
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import anthropic
from claude_agent_sdk import (
    AssistantMessage,
    CanUseToolShadowedWarning,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolAnnotations,
    ToolPermissionContext,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from invoice_agent.config import Config
from invoice_agent.durations import parse_entry
from invoice_agent.invoice import (
    InvoiceError,
    InvoicePlan,
    Workspace,
    confirm_prompt,
    create_invoice,
    is_approval,
    plan_invoice,
)
from invoice_agent.merge import MergeError
from invoice_agent.report import (
    describe_api_error,
    format_merge,
    format_plan,
    format_problems,
    format_verify,
    format_week,
)
from invoice_agent.timesheet import TimesheetReadError, TimesheetWeek, make_client, merge_weeks, read_folder
from invoice_agent.verify import VerifyResult, verify_totals
from invoice_agent.weeks import Month

SERVER = "invoice"
CREATE_INVOICE = f"mcp__{SERVER}__create_invoice"
AUTO_APPROVED = [
    f"mcp__{SERVER}__{name}" for name in ("read_timesheets", "verify_hours", "merge_timesheets", "preview_invoice")
]

# The SDK warns that auto-approved tools skip can_use_tool. That's the intent here:
# only create_invoice is meant to reach the approval prompt.
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

# The agent's instructions. {today} is filled in when a session starts.
SYSTEM_PROMPT_FILE = Path(__file__).parent / "prompts" / "system.md"

FOLDER_MONTH = {
    "folder": {"type": "string", "description": "Screenshot folder inside data/, e.g. data/2026-09"},
    "month": {"type": "string", "description": "Billing month as YYYY-MM, e.g. 2026-09"},
}
WEEKLY_TOTALS = {
    "weekly_totals": {
        "type": "array",
        "items": {"type": "string"},
        "description": "The user's weekly totals as they typed them (e.g. '15h 20m'), one per invoice week, "
        "earliest first",
    }
}
FOLDER_MONTH_SCHEMA = {"type": "object", "properties": FOLDER_MONTH, "required": ["folder", "month"]}
HOURS_SCHEMA = {
    "type": "object",
    "properties": {**FOLDER_MONTH, **WEEKLY_TOTALS},
    "required": ["folder", "month", "weekly_totals"],
}


def system_prompt(today: date) -> str:
    return SYSTEM_PROMPT_FILE.read_text().replace("{today}", f"{today:%A, %B %-d, %Y}")


class ToolInputError(ValueError):
    """Bad tool input; the message is shown to Claude so it can correct itself."""


@dataclass(frozen=True)
class PreparedInvoice:
    """An invoice worked out from verified hours, ready to show for approval or to create."""

    plan: InvoicePlan
    pdf_path: Path


class ApprovalGate:
    """Holds the invoices the user approved, keyed by the exact request; each can be used once.

    The approved plan itself is stored, so what gets created is exactly what
    the user saw, even if the screenshots change in between.
    """

    def __init__(self) -> None:
        self._approved: dict[str, PreparedInvoice] = {}

    @staticmethod
    def _key(args: dict[str, Any]) -> str:
        return json.dumps({k: args.get(k) for k in ("folder", "month", "weekly_totals")}, sort_keys=True)

    def approve(self, args: dict[str, Any], invoice: PreparedInvoice) -> None:
        self._approved[self._key(args)] = invoice

    def consume(self, args: dict[str, Any]) -> PreparedInvoice | None:
        return self._approved.pop(self._key(args), None)


class InvoiceTools:
    """The agent's tools, plus the approval callback for create_invoice."""

    def __init__(
        self,
        config: Config,
        data_root: Path,
        workspace_factory: Callable[[Config], Workspace],
        ask: Callable[[str], str] = input,
        client_factory: Callable[[], anthropic.Anthropic] = make_client,
    ) -> None:
        self.config = config
        self.data_root = data_root.resolve()
        self.workspace_factory = workspace_factory
        self.ask = ask
        self.client_factory = client_factory
        self.gate = ApprovalGate()

    # --- shared steps ---------------------------------------------------------

    def folder(self, text: str) -> Path:
        # Relative paths may include the leading "data/" or not: "data/2026-09" or "2026-09".
        path = Path(text)
        if not path.is_absolute():
            base = self.data_root.parent if path.parts[:1] == (self.data_root.name,) else self.data_root
            path = base / path
        path = path.resolve()
        if not path.is_relative_to(self.data_root):
            raise ToolInputError(f"{text} is outside {self.data_root.name}/; screenshot folders must be inside it")
        if not path.is_dir():
            raise ToolInputError(f"No folder at {text}")
        return path

    @staticmethod
    def month(text: str) -> Month:
        try:
            return Month.parse(text)
        except ValueError as err:
            raise ToolInputError(str(err)) from None

    def read(self, folder: Path) -> list[TimesheetWeek]:
        try:
            return read_folder(folder, self.client_factory())
        except TimesheetReadError as err:
            raise ToolInputError(str(err)) from None
        except anthropic.AnthropicError as err:
            raise ToolInputError(f"Couldn't read the screenshots: {describe_api_error(err)}") from None

    def verify(self, args: dict[str, Any]) -> tuple[Path, VerifyResult]:
        folder, month = self.folder(args["folder"]), self.month(args["month"])
        try:
            typed = [parse_entry(t) for t in args["weekly_totals"]]
            return folder, verify_totals(month, typed, self.read(folder))
        except ToolInputError:
            raise
        except ValueError as err:
            raise ToolInputError(str(err)) from None

    def prepare(self, args: dict[str, Any]) -> PreparedInvoice:
        folder, result = self.verify(args)
        if not result.ok:
            raise ToolInputError(format_verify(result))
        plan = plan_invoice(result, self.config.name_format)
        return PreparedInvoice(plan, folder / plan.month.invoice_pdf_name)

    def describe(self, invoice: PreparedInvoice) -> str:
        return format_plan(invoice.plan, self.config, invoice.pdf_path)

    # --- tool bodies (plain functions, run in a worker thread) ------------------

    def read_timesheets(self, args: dict[str, Any]) -> str:
        folder, month = self.folder(args["folder"]), self.month(args["month"])
        weeks = sorted(self.read(folder), key=lambda w: w.week_start)
        return "\n".join(format_week(w, month) for w in weeks)

    def verify_hours(self, args: dict[str, Any]) -> str:
        return format_verify(self.verify(args)[1])

    def merge_timesheets(self, args: dict[str, Any]) -> str:
        folder, month = self.folder(args["folder"]), self.month(args["month"])
        weeks = self.read(folder)
        try:
            result = merge_weeks(weeks, month, folder / month.timesheets_pdf_name)
        except MergeError as err:
            raise ToolInputError(str(err)) from None
        return "\n".join([*format_problems(weeks), format_merge(result)])

    def preview_invoice(self, args: dict[str, Any]) -> str:
        return self.describe(self.prepare(args)) + "\nPreview only: nothing was created."

    def create_invoice(self, args: dict[str, Any]) -> str:
        invoice = self.gate.consume(args)
        if invoice is None:
            raise ToolInputError("Not approved: the user must approve create_invoice in the terminal.")
        try:
            workspace = self.workspace_factory(self.config)
            created = create_invoice(invoice.plan, self.config, workspace, invoice.pdf_path)
        except (InvoiceError, FileNotFoundError) as err:
            raise ToolInputError(str(err)) from None
        return f"Created {created.url}\nSaved {created.pdf}"

    # --- approval -------------------------------------------------------------

    async def approve_tool(
        self, tool_name: str, input_data: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Called by the SDK for any tool call that isn't auto-approved."""
        if tool_name != CREATE_INVOICE:
            return PermissionResultDeny(message=f"{tool_name} isn't available to this agent.")
        try:
            invoice = await asyncio.to_thread(self.prepare, input_data)
        except ToolInputError as err:
            return PermissionResultDeny(message=f"Can't create this invoice: {err}")
        print(f"\n{self.describe(invoice)}")
        if not is_approval(await asyncio.to_thread(self.ask, confirm_prompt(invoice.plan))):
            return PermissionResultDeny(message="The user declined to create the invoice.")
        self.gate.approve(input_data, invoice)
        return PermissionResultAllow(updated_input=input_data)

    # --- SDK wiring -----------------------------------------------------------

    def server(self):
        return create_sdk_mcp_server(name=SERVER, version="1.0.0", tools=self.sdk_tools())

    def sdk_tools(self) -> list:
        def wrap(name: str, description: str, schema: dict, body: Callable[[dict], str], read_only: bool):
            @tool(name, description, schema, annotations=ToolAnnotations(readOnlyHint=read_only))
            async def handler(args: dict[str, Any]) -> dict[str, Any]:
                try:
                    text = await asyncio.to_thread(body, args)
                except ToolInputError as err:
                    return {"content": [{"type": "text", "text": str(err)}], "is_error": True}
                return {"content": [{"type": "text", "text": text}]}

            return handler

        return [
            wrap(
                "read_timesheets",
                "Show what each screenshot in a folder contains: its week, daily totals, weekly total, and the "
                "time inside the month. Screenshots are read once and cached.",
                FOLDER_MONTH_SCHEMA,
                self.read_timesheets,
                read_only=True,
            ),
            wrap(
                "verify_hours",
                "Check the user's weekly totals against the screenshots. Returns a table with each week's status "
                "and the hours that would go on the invoice.",
                HOURS_SCHEMA,
                self.verify_hours,
                read_only=True,
            ),
            wrap(
                "merge_timesheets",
                "Combine the month's screenshots, earliest week first, into 'Timesheets MM-YYYY.pdf' in the folder.",
                FOLDER_MONTH_SCHEMA,
                self.merge_timesheets,
                read_only=False,
            ),
            wrap(
                "preview_invoice",
                "Show exactly what create_invoice would write, without creating anything. Fails unless all weeks "
                "verify.",
                HOURS_SCHEMA,
                self.preview_invoice,
                read_only=True,
            ),
            wrap(
                "create_invoice",
                "Copy the Google Sheets invoice template, fill in the month, and save the invoice PDF. The user "
                "approves each call in the terminal.",
                HOURS_SCHEMA,
                self.create_invoice,
                read_only=False,
            ),
        ]

    def options(self, model: str, project_root: Path) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            model=model,
            effort="medium",
            system_prompt=system_prompt(date.today()),
            tools=[],  # no built-in tools: only the invoice tools above
            mcp_servers={SERVER: self.server()},
            allowed_tools=AUTO_APPROVED,  # create_invoice deliberately left out
            permission_mode="default",  # never auto mode or bypass
            can_use_tool=self.approve_tool,
            setting_sources=[],  # ignore personal Claude Code settings
            cwd=project_root,
            env={"ENABLE_TOOL_SEARCH": "false"},  # five tools: load them all up front
            max_turns=40,
            max_budget_usd=2.0,
        )


async def chat(tools: InvoiceTools, model: str, project_root: Path, first_message: str | None = None) -> None:
    """Terminal chat with the agent. Type 'exit' or press Ctrl-D to quit."""
    print("Invoice agent. Describe the month, e.g.:")
    print('  "September: 2h 30m, 10h, 5h 30m, 7h 30m, 15h 20m. Screenshots are in data/2026-09."')
    print("Type 'exit' to quit.")
    cost = 0.0
    async with ClaudeSDKClient(options=tools.options(model, project_root)) as client:
        message = first_message
        while True:
            if message is None:
                try:
                    message = (await asyncio.to_thread(input, "\nyou> ")).strip()
                except EOFError:
                    break
            if message.lower() in ("exit", "quit"):
                break
            if message:
                await client.query(message)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                print(f"\nagent> {block.text}")
                            elif isinstance(block, ToolUseBlock):
                                print(f"  [{block.name.removeprefix(f'mcp__{SERVER}__')}]")
                    elif isinstance(msg, ResultMessage):
                        # A running total for the whole session, not this turn's cost.
                        if msg.total_cost_usd is not None:
                            cost = msg.total_cost_usd
                        if msg.is_error:
                            print(f"\n(agent stopped: {msg.subtype}{': ' + '; '.join(msg.errors) if msg.errors else ''})")
            message = None
    print(f"\nSession cost: ${cost:.2f}")
