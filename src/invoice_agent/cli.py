"""Phase 1 command-line entry point. Only `read` and the default `merge` call the Claude API.

    invoice-agent read data/2026-09 --month 2026-09
    invoice-agent merge data/2026-09 --month 2026-09
    invoice-agent merge data/2026-09 --month 2026-09 --manual
    invoice-agent merge data/2026-09 --month 2026-09 --weeks 2026-08-31,2026-09-07,...
    invoice-agent verify data/2026-09 --month 2026-09 --hours "2h 30m, 15h 20m, 10h, 8h 05m, 12h"
    invoice-agent invoice data/2026-09 --month 2026-09 --hours "2h 30m, 15h 20m, 10h, 8h 05m, 12h" --dry-run
    invoice-agent weeks --month 2026-09
    invoice-agent agent ["September: 2h 30m, 10h, ... Screenshots are in data/2026-09."]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from invoice_agent.config import DEFAULT_PATH, Config, load_config
from invoice_agent.durations import parse_entry
from invoice_agent.invoice import InvoiceError, confirm_prompt, create_invoice, is_approval, plan_invoice
from invoice_agent.merge import MergeError, Screenshot, find_images, merge_screenshots
from invoice_agent.report import (
    describe_api_error,
    format_merge,
    format_plan,
    format_problems,
    format_verify,
    format_week,
    format_weeks,
)
from invoice_agent.timesheet import TimesheetReadError, TimesheetWeek, make_client, merge_weeks, read_folder
from invoice_agent.verify import VerifyResult, verify_totals
from invoice_agent.weeks import Month, week_start_for


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

    agent = sub.add_parser("agent", help="chat with the invoice agent (Claude Agent SDK)")
    agent.add_argument("message", nargs="?", help="optional first message")
    agent.add_argument("--config", type=Path, default=DEFAULT_PATH)

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
    if args.command == "agent":
        return _cmd_agent(args)
    return _cmd_merge(args)


def _cmd_weeks(month: Month) -> int:
    print(format_weeks(month))
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
    sheets = _read_all(args.folder)
    if sheets is None:
        return None
    try:
        result = verify_totals(args.month, typed, sheets)
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return None
    print(format_verify(result))
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
    pdf_path = args.out or args.folder / plan.month.invoice_pdf_name
    print()
    print(format_plan(plan, config, pdf_path))
    if args.dry_run:
        print("\nDry run: nothing was created.")
        return 0

    if not is_approval(input(confirm_prompt(plan))):
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


def _cmd_agent(args: argparse.Namespace) -> int:
    # Imported here: the Agent SDK is only needed for this command.
    import asyncio

    from invoice_agent.agent import InvoiceTools, chat

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    project_root = args.config.resolve().parent
    tools = InvoiceTools(config, project_root / "data", _google_workspace)
    try:
        asyncio.run(chat(tools, config.agent_model, project_root, args.message))
    except KeyboardInterrupt:
        print()
    return 0


def _google_workspace(config: Config):
    # Imported here so commands that don't touch Google don't load its libraries.
    from invoice_agent.google_auth import get_credentials
    from invoice_agent.google_workspace import GoogleWorkspace

    return GoogleWorkspace(get_credentials(config.credentials_file, config.token_file))


def _cmd_read(args: argparse.Namespace) -> int:
    weeks = _read_all(args.folder, refresh=args.refresh)
    if weeks is None:
        return 1
    for week in sorted(weeks, key=lambda w: w.week_start):
        print(format_week(week, args.month))
    return 0 if all(w.ok for w in weeks) else 1


def _read_all(folder: Path, refresh: bool = False) -> list[TimesheetWeek] | None:
    """Read every screenshot in `folder`, or print why not and return None."""
    try:
        return read_folder(
            folder, make_client(), refresh=refresh, on_read=lambda p: print(f"Reading {p.name}...", file=sys.stderr)
        )
    except TimesheetReadError as err:
        print(f"Error: {err}", file=sys.stderr)
    except anthropic.AnthropicError as err:
        print(f"Error: {describe_api_error(err)}", file=sys.stderr)
    return None


def _cmd_merge(args: argparse.Namespace) -> int:
    images = find_images(args.folder)
    if not images:
        print(f"No PNG/JPG images in {args.folder}", file=sys.stderr)
        return 1

    out = args.out or args.folder / args.month.timesheets_pdf_name
    try:
        if args.weeks or args.manual:
            dates = _week_dates(args, images)
            if dates is None:
                return 1
            shots = [Screenshot(path, week_start_for(date.fromisoformat(d))) for path, d in zip(images, dates) if d]
            result = merge_screenshots(shots, args.month, out)
        else:
            weeks = _read_all(args.folder)
            if weeks is None:
                return 1
            for warning in format_problems(weeks):
                print(f"  {warning}")
            result = merge_weeks(weeks, args.month, out)
    except (MergeError, ValueError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    print(format_merge(result))
    return 0


def _week_dates(args: argparse.Namespace, images: list[Path]) -> list[str] | None:
    """One date per image, from --weeks or by asking; None if --weeks has the wrong count."""
    if not args.weeks:
        return _prompt_for_weeks(images)
    dates = [d.strip() for d in args.weeks.split(",")]
    if len(dates) != len(images):
        print(f"Got {len(dates)} dates for {len(images)} images:", file=sys.stderr)
        _list_images(images, file=sys.stderr)
        return None
    return dates


def _list_images(images: list[Path], file=sys.stdout) -> None:
    for i, path in enumerate(images, 1):
        print(f"  {i}. {path.name}", file=file)


def _prompt_for_weeks(images: list[Path]) -> list[str]:
    print("For each screenshot, enter any date in the week it shows as YYYY-MM-DD (blank to skip).")
    return [input(f"  {path.name}: ").strip() for path in images]


if __name__ == "__main__":
    sys.exit(main())
