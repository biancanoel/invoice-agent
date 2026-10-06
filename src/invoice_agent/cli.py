"""Phase 1 command-line entry point. Only `read` and the default `merge` call the Claude API.

    invoice-agent read data/2026-09 --month 2026-09
    invoice-agent merge data/2026-09 --month 2026-09
    invoice-agent merge data/2026-09 --month 2026-09 --manual
    invoice-agent merge data/2026-09 --month 2026-09 --weeks 2026-08-31,2026-09-07,...
    invoice-agent verify data/2026-09 --month 2026-09 --hours "2h 30m, 15h 20m, 10h, 8h 05m, 12h"
    invoice-agent invoice data/2026-09 --month 2026-09 --hours "2h 30m, 15h 20m, 10h, 8h 05m, 12h" --dry-run
    invoice-agent weeks --month 2026-09
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from invoice_agent.invoice import InvoiceError, InvoicePlan, create_invoice, plan_invoice
from invoice_agent.merge import MergeError, Screenshot, find_images, merge_screenshots
from invoice_agent.config import DEFAULT_PATH, Config, load_config
from invoice_agent.durations import format_duration, parse_entry, to_hours
from invoice_agent.timesheet import TimesheetReadError, TimesheetWeek, make_client, read_timesheet
from invoice_agent.verify import VerifyResult, verify_totals
from invoice_agent.weeks import Month, billing_weeks, week_start_for


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="invoice-agent")
    sub = parser.add_subparsers(dest="command", required=True)

    merge = sub.add_parser("merge", help="combine weekly timesheet screenshots into one PDF")
    merge.add_argument("folder", type=Path)
    merge.add_argument("--month", required=True, type=Month.parse, help="YYYY-MM")
    how = merge.add_mutually_exclusive_group()
    how.add_argument(
        "--weeks",
        help="skip reading the screenshots: comma-separated YYYY-MM-DD dates, one per image "
        "in the order listed by the command; any date inside the week works",
    )
    how.add_argument("--manual", action="store_true", help="skip reading the screenshots and prompt for each week")
    merge.add_argument("--out", type=Path, help="defaults to '<folder>/Timesheets MM-YYYY.pdf'")

    read = sub.add_parser("read", help="read each screenshot's week and daily hours (uses the Claude API)")
    read.add_argument("folder", type=Path)
    read.add_argument("--month", type=Month.parse, help="YYYY-MM; also show hours inside this month")
    read.add_argument("--refresh", action="store_true", help="ignore cached results and re-read")

    verify = sub.add_parser("verify", help="check typed weekly hours against the screenshots")
    verify.add_argument("folder", type=Path)
    verify.add_argument("--month", required=True, type=Month.parse, help="YYYY-MM")
    verify.add_argument(
        "--hours",
        required=True,
        help="each week's total from the bottom-right of its screenshot, earliest week first, "
        'comma-separated, e.g. "2h 30m, 15h 20m, 10h, 8h 05m, 12h". Decimal hours like 1.5 also work',
    )

    invoice = sub.add_parser("invoice", help="verify the hours, then create the invoice sheet and PDF")
    invoice.add_argument("folder", type=Path)
    invoice.add_argument("--month", required=True, type=Month.parse, help="YYYY-MM")
    invoice.add_argument("--hours", required=True, help="same as for `verify`")
    invoice.add_argument("--dry-run", action="store_true", help="show what would be written without touching Google")
    invoice.add_argument("--out", type=Path, help="PDF path; defaults to '<folder>/Invoice MM-YYYY.pdf'")
    invoice.add_argument("--config", type=Path, default=DEFAULT_PATH)

    weeks = sub.add_parser("weeks", help="show the invoice line-item weeks for a month")
    weeks.add_argument("--month", required=True, type=Month.parse, help="YYYY-MM")

    args = parser.parse_args(argv)
    if args.command == "weeks":
        return _cmd_weeks(args.month)
    if args.command == "read":
        return _cmd_read(args)
    if args.command == "verify":
        return _cmd_verify(args)
    if args.command == "invoice":
        return _cmd_invoice(args)
    return _cmd_merge(args)


def _cmd_weeks(month: Month) -> int:
    print(f"Invoice {month.invoice_number}")
    for week in billing_weeks(month):
        print(f"  {week.label:<18} (timesheet week of {week.week_start:%a %b %d})")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    result = _verify(args)
    return 0 if result and result.ok else 1


def _verify(args: argparse.Namespace) -> VerifyResult | None:
    """Read the screenshots and check --hours against them, printing the table."""
    try:
        typed = [parse_entry(h) for h in args.hours.split(",")]
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return None
    images = find_images(args.folder)
    if not images:
        print(f"No PNG/JPG images in {args.folder}", file=sys.stderr)
        return None
    sheets = _read_all(images)
    if sheets is None:
        return None
    try:
        result = verify_totals(args.month, typed, sheets)
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return None
    _print_verify(result)
    return result


def _cmd_invoice(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except FileNotFoundError as err:
        if not args.dry_run:
            print(f"Error: {err}", file=sys.stderr)
            return 1
        config = Config(template_id="(not configured)", folder_id="(not configured)")
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    result = _verify(args)
    if result is None or not result.ok:
        return 1
    plan = plan_invoice(result, config.name_format)
    pdf_path = args.out or args.folder / f"Invoice {plan.invoice_number}.pdf"
    print()
    _print_plan(plan, config, pdf_path)
    if args.dry_run:
        print("\nDry run: nothing was created.")
        return 0

    if input(f"\nCreate '{plan.name}'? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Cancelled; nothing was created.")
        return 1
    try:
        workspace = _google_workspace(config)
        created = create_invoice(plan, config, workspace, pdf_path)
    except (InvoiceError, FileNotFoundError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    print(f"Created {created.url}")
    print(f"Saved {created.pdf}")
    return 0


def _google_workspace(config: Config):
    # Imported here so commands that don't touch Google don't load its libraries.
    from invoice_agent.google_auth import get_credentials
    from invoice_agent.google_workspace import GoogleWorkspace

    return GoogleWorkspace(get_credentials(config.credentials_file, config.token_file))


def _print_plan(plan: InvoicePlan, config: Config, pdf_path: Path) -> None:
    layout = config.layout
    print(f"Invoice plan: '{plan.name}' (copy of template {config.template_id})")
    for cell, value in plan.cells(layout)[:3]:
        print(f"  {cell:<5} {value}")
    print(f"        (due date shows as {plan.due_date:%-m/%-d/%Y})")
    for i, line in enumerate(plan.lines):
        row = layout.first_line_row + i
        print(f"  {layout.description_column}{row:<4} {line.description:<20} {layout.hours_column}{row:<4} {line.hours:g}")
    unused = layout.line_rows - len(plan.lines)
    if unused:
        first = layout.first_line_row + len(plan.lines)
        print(f"  rows {first}-{layout.first_line_row + layout.line_rows - 1} cleared (blank description, 0 hours)")
    print(f"  Total hours: {plan.total_hours:g}  (the sheet's formulas compute the amounts)")
    print(f"  PDF: {pdf_path}")


STATUS_LABELS = {
    "match": "ok",
    "mismatch": "MISMATCH",
    "no_screenshot": "NO SCREENSHOT",
    "unreadable": "UNREADABLE",
    "duplicate": "DUPLICATE",
}


def _print_verify(result: VerifyResult) -> None:
    month_name = f"{result.month.first_day:%b}"
    print(f"Invoice {result.month.invoice_number}")
    print(f"  {'Week':<18} {'You entered':>12} {'Screenshot':>11} {'In ' + month_name:>9} {'Invoice hrs':>12}")
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
        print(line)
    total = result.invoice_minutes
    print(f"  {'Invoice total':<42} {format_duration(total):>9} {to_hours(total):>12g}")
    for sheet in result.ignored:
        print(f"  (ignored {sheet.image.name}: week of {sheet.week_start:%b %d} is outside the month)")
    print("All weeks match." if result.ok else "Fix the flagged weeks before creating the invoice.")


def _cmd_read(args: argparse.Namespace) -> int:
    images = find_images(args.folder)
    if not images:
        print(f"No PNG/JPG images in {args.folder}", file=sys.stderr)
        return 1
    weeks = _read_all(images, refresh=args.refresh)
    if weeks is None:
        return 1
    for week in sorted(weeks, key=lambda w: w.week_start):
        _print_week(week, args.month)
    return 0 if all(w.ok for w in weeks) else 1


def _print_week(week: TimesheetWeek, month: Month | None) -> None:
    end = week.week_start + timedelta(days=6)
    line = f"{week.week_start:%b %d} - {end:%b %d}: {format_duration(week.total_minutes)} total"
    if month:
        in_month = week.minutes_in(month.first_day, month.last_day)
        line += f", {format_duration(in_month)} in {month.first_day:%B}"
    print(f"{line}  [{week.image.name}]")
    days = "  ".join(f"{d:%a %d} {format_duration(m)}" for d, m in sorted(week.day_minutes.items()))
    print(f"    {days}")
    if week.confidence != "high":
        print(f"    confidence: {week.confidence}. {week.notes}")
    for problem in week.problems:
        print(f"    PROBLEM: {problem}")


def _read_all(images: list[Path], refresh: bool = False) -> list[TimesheetWeek] | None:
    """Read every image, or print why not and return None."""
    weeks = []
    try:
        client = make_client()
        for path in images:
            print(f"Reading {path.name}...", file=sys.stderr)
            weeks.append(read_timesheet(path, client, refresh=refresh))
    except TimesheetReadError as err:
        print(f"Error: {err}", file=sys.stderr)
        return None
    except anthropic.AuthenticationError:
        print("Error: the Anthropic API key was rejected. Check ANTHROPIC_API_KEY in .env.", file=sys.stderr)
        return None
    except anthropic.APIConnectionError:
        print("Error: couldn't reach the Anthropic API. Check your connection.", file=sys.stderr)
        return None
    except anthropic.APIStatusError as err:
        print(f"Error from the Anthropic API ({err.status_code}): {_api_message(err)}", file=sys.stderr)
        return None
    except anthropic.AnthropicError as err:
        # A missing key is reported when the client is constructed, before any request.
        print(f"Error: {err}\nIs ANTHROPIC_API_KEY set in .env?", file=sys.stderr)
        return None
    return weeks


def _api_message(err: anthropic.APIStatusError) -> str:
    body = err.body if isinstance(err.body, dict) else {}
    return body.get("error", {}).get("message") or str(err)


def _cmd_merge(args: argparse.Namespace) -> int:
    images = find_images(args.folder)
    if not images:
        print(f"No PNG/JPG images in {args.folder}", file=sys.stderr)
        return 1

    if args.weeks or args.manual:
        if args.weeks:
            dates = [d.strip() for d in args.weeks.split(",")]
            if len(dates) != len(images):
                print(f"Got {len(dates)} dates for {len(images)} images:", file=sys.stderr)
                _list_images(images, file=sys.stderr)
                return 1
        else:
            dates = _prompt_for_weeks(images)
        try:
            starts = [(path, week_start_for(date.fromisoformat(d))) for path, d in zip(images, dates) if d]
        except ValueError as err:
            print(f"Error: {err}", file=sys.stderr)
            return 1
    else:
        weeks = _read_all(images)
        if weeks is None:
            return 1
        for week in weeks:
            for problem in week.problems:
                print(f"  WARNING ({week.image.name}): {problem}")
        starts = [(w.image, w.week_start) for w in weeks]

    try:
        shots = [Screenshot(path, monday) for path, monday in starts]
        out = args.out or args.folder / f"Timesheets {args.month.invoice_number}.pdf"
        result = merge_screenshots(shots, args.month, out)
    except (MergeError, ValueError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    print(f"Wrote {result.output} ({len(result.included)} pages):")
    for shot in result.included:
        print(f"  week of {shot.week_start:%b %d}: {shot.path.name}")
    for shot in result.skipped:
        print(f"  skipped (outside month): {shot.path.name}")
    for monday in result.missing_weeks:
        print(f"  WARNING: no screenshot for the week of {monday:%b %d}")
    return 0


def _list_images(images: list[Path], file=sys.stdout) -> None:
    for i, path in enumerate(images, 1):
        print(f"  {i}. {path.name}", file=file)


def _prompt_for_weeks(images: list[Path]) -> list[str]:
    print("For each screenshot, enter any date in the week it shows as YYYY-MM-DD (blank to skip).")
    return [input(f"  {path.name}: ").strip() for path in images]


if __name__ == "__main__":
    sys.exit(main())
