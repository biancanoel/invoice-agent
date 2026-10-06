"""Plain-text reports shared by the CLI and the agent's tools.

Each function returns text instead of printing, so the CLI can print it and
the agent can hand the exact same text to Claude as a tool result.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import anthropic

from invoice_agent.config import Config
from invoice_agent.durations import format_duration, to_hours
from invoice_agent.invoice import InvoicePlan
from invoice_agent.merge import MergeResult
from invoice_agent.timesheet import TimesheetWeek
from invoice_agent.verify import VerifyResult
from invoice_agent.weeks import Month, billing_weeks

STATUS_LABELS = {
    "match": "ok",
    "mismatch": "MISMATCH",
    "no_screenshot": "NO SCREENSHOT",
    "unreadable": "UNREADABLE",
    "duplicate": "DUPLICATE",
}


def format_weeks(month: Month) -> str:
    lines = [f"Invoice {month.invoice_number}"]
    for week in billing_weeks(month):
        lines.append(f"  {week.label:<18} (timesheet week of {week.week_start:%a %b %d})")
    return "\n".join(lines)


def format_week(week: TimesheetWeek, month: Month | None) -> str:
    end = week.week_start + timedelta(days=6)
    line = f"{week.week_start:%b %d} - {end:%b %d}: {format_duration(week.total_minutes)} total"
    if month:
        in_month = week.minutes_in(month.first_day, month.last_day)
        line += f", {format_duration(in_month)} in {month.first_day:%B}"
    lines = [f"{line}  [{week.image.name}]"]
    lines.append("    " + "  ".join(f"{d:%a %d} {format_duration(m)}" for d, m in sorted(week.day_minutes.items())))
    if week.confidence != "high":
        lines.append(f"    confidence: {week.confidence}. {week.notes}")
    lines.extend(f"    PROBLEM: {problem}" for problem in week.problems)
    return "\n".join(lines)


def format_verify(result: VerifyResult) -> str:
    month_name = f"{result.month.first_day:%b}"
    lines = [
        f"Invoice {result.month.invoice_number}",
        f"  {'Week':<18} {'You entered':>12} {'Screenshot':>11} {'In ' + month_name:>9} {'Invoice hrs':>12}",
    ]
    for check in result.checks:
        shown = "-" if check.screenshot_minutes is None else format_duration(check.screenshot_minutes)
        in_month = "-" if check.invoice_minutes is None else format_duration(check.invoice_minutes)
        hours = "-" if check.invoice_minutes is None else f"{to_hours(check.invoice_minutes):g}"
        line = (
            f"  {check.week.label:<18} {format_duration(check.typed_minutes):>12} {shown:>11}"
            f" {in_month:>9} {hours:>12}  {STATUS_LABELS[check.status]}"
        )
        if check.detail:
            line += f": {check.detail}"
        lines.append(line)
    total = result.invoice_minutes
    lines.append(f"  {'Invoice total':<42} {format_duration(total):>9} {to_hours(total):>12g}")
    for sheet in result.ignored:
        lines.append(f"  (ignored {sheet.image.name}: week of {sheet.week_start:%b %d} is outside the month)")
    lines.append("All weeks match." if result.ok else "Fix the flagged weeks before creating the invoice.")
    return "\n".join(lines)


def format_plan(plan: InvoicePlan, config: Config, pdf_path: Path) -> str:
    layout = config.layout
    lines = [f"Invoice plan: '{plan.name}' (copy of template {config.template_id})"]
    lines.extend(f"  {cell:<5} {value}" for cell, value in plan.cells(layout)[:3])
    lines.append(f"        (due date shows as {plan.due_date:%-m/%-d/%Y})")
    for i, line in enumerate(plan.lines):
        row = layout.first_line_row + i
        lines.append(
            f"  {layout.description_column}{row:<4} {line.description:<20} {layout.hours_column}{row:<4} {line.hours:g}"
        )
    if layout.line_rows > len(plan.lines):
        first = layout.first_line_row + len(plan.lines)
        lines.append(f"  rows {first}-{layout.first_line_row + layout.line_rows - 1} cleared (blank description, 0 hours)")
    lines.append(f"  Total hours: {plan.total_hours:g}  (the sheet's formulas compute the amounts)")
    lines.append(f"  PDF: {pdf_path}")
    return "\n".join(lines)


def format_merge(result: MergeResult) -> str:
    lines = [f"Wrote {result.output} ({len(result.included)} pages):"]
    lines.extend(f"  week of {shot.week_start:%b %d}: {shot.path.name}" for shot in result.included)
    lines.extend(f"  skipped (outside month): {shot.path.name}" for shot in result.skipped)
    lines.extend(f"  WARNING: no screenshot for the week of {monday:%b %d}" for monday in result.missing_weeks)
    return "\n".join(lines)


def describe_api_error(err: anthropic.AnthropicError) -> str:
    """A readable explanation of an Anthropic API failure while reading screenshots."""
    if isinstance(err, anthropic.AuthenticationError):
        return "the Anthropic API key was rejected. Check ANTHROPIC_API_KEY in .env."
    if isinstance(err, anthropic.APIConnectionError):
        return "couldn't reach the Anthropic API. Check your connection."
    if isinstance(err, anthropic.APIStatusError):
        body = err.body if isinstance(err.body, dict) else {}
        message = body.get("error", {}).get("message") or str(err)
        return f"the Anthropic API returned an error ({err.status_code}): {message}"
    # A missing key is reported when the client is constructed, before any request.
    return f"{err}. Is ANTHROPIC_API_KEY set in .env?"
