"""create_invoice: copy the Google Sheet template, fill in the month, export a PDF.

Split in two so the dry run and the real run can't drift apart:
- plan_invoice works out every cell value from a passing verification (pure).
- create_invoice applies a plan through a Google gateway (side effects).

The sheet's own formulas compute line totals, subtotal, and total; this only
writes descriptions, hours, the invoice number, and dates. Values are written
RAW so Sheets doesn't reinterpret them (e.g. "09-2026" as a date).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from invoice_agent.config import Config, Layout
from invoice_agent.durations import to_hours
from invoice_agent.verify import VerifyResult
from invoice_agent.weeks import Month

SHEETS_EPOCH = date(1899, 12, 30)  # day 0 for date serial numbers in Google Sheets


@dataclass(frozen=True)
class InvoiceLine:
    description: str
    hours: float


@dataclass(frozen=True)
class InvoicePlan:
    month: Month
    name: str  # Drive file name for the new sheet
    invoice_number: str
    submitted_on: date
    due_date: date
    lines: list[InvoiceLine]

    @property
    def total_hours(self) -> float:
        return round(sum(line.hours for line in self.lines), 2)

    def cells(self, layout: Layout) -> list[tuple[str, str | float | int]]:
        """Every (A1 cell, value) to write, including blanks for unused line rows."""
        if len(self.lines) > layout.line_rows:
            raise ValueError(f"{len(self.lines)} line items but the template only has {layout.line_rows} rows")
        d = self.submitted_on
        cells: list[tuple[str, str | float | int]] = [
            (layout.submitted_on, f"Submitted on {d.month}/{d.day}/{d.year}"),
            (layout.invoice_number, self.invoice_number),
            (layout.due_date, sheets_date(self.due_date)),
        ]
        for i in range(layout.line_rows):
            row = layout.first_line_row + i
            line = self.lines[i] if i < len(self.lines) else InvoiceLine("", 0)
            cells.append((f"{layout.description_column}{row}", line.description))
            cells.append((f"{layout.hours_column}{row}", line.hours))
        return cells


class InvoiceError(RuntimeError):
    pass


def sheets_date(d: date) -> int:
    """Date as a Google Sheets serial number, so the cell's date format applies."""
    return (d - SHEETS_EPOCH).days


def plan_invoice(result: VerifyResult, name_format: str) -> InvoicePlan:
    """Work out the invoice for a month whose hours have all been verified."""
    if not result.ok:
        raise InvoiceError("Hours haven't been verified against the screenshots; fix the flagged weeks first")
    month = result.month
    last_day = month.last_day
    return InvoicePlan(
        month=month,
        name=name_format.format(month_name=calendar.month_name[month.month], year=month.year),
        invoice_number=month.invoice_number,
        submitted_on=last_day,
        due_date=last_day,
        lines=[InvoiceLine(c.week.label, to_hours(c.invoice_minutes or 0)) for c in result.checks],
    )


class Workspace(Protocol):
    """The Google Drive/Sheets calls create_invoice needs (faked in tests)."""

    def find_file(self, name: str, folder_id: str) -> str | None: ...
    def copy_file(self, file_id: str, name: str, folder_id: str) -> str: ...
    def write_cells(self, spreadsheet_id: str, sheet: str, cells: list[tuple[str, str | float | int]]) -> None: ...
    def export_pdf(self, spreadsheet_id: str, sheet: str) -> bytes: ...


@dataclass(frozen=True)
class CreatedInvoice:
    spreadsheet_id: str
    url: str
    pdf: Path


def create_invoice(plan: InvoicePlan, config: Config, workspace: Workspace, pdf_path: Path) -> CreatedInvoice:
    """Copy the template, fill it in, and save the PDF. Refuses to overwrite an existing invoice."""
    cells = plan.cells(config.layout)  # validate before touching Drive
    existing = workspace.find_file(plan.name, config.folder_id)
    if existing:
        raise InvoiceError(f"'{plan.name}' already exists: {sheet_url(existing)}. Delete or rename it first.")

    spreadsheet_id = workspace.copy_file(config.template_id, plan.name, config.folder_id)
    url = sheet_url(spreadsheet_id)
    try:
        workspace.write_cells(spreadsheet_id, config.layout.sheet, cells)
        pdf = workspace.export_pdf(spreadsheet_id, config.layout.sheet)
    except Exception as err:
        raise InvoiceError(f"Created {url} but couldn't finish it: {err}") from err

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(pdf)
    return CreatedInvoice(spreadsheet_id=spreadsheet_id, url=url, pdf=pdf_path)


def sheet_url(spreadsheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
