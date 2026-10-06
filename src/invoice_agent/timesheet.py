"""read_timesheet: read one weekly timesheet screenshot with Claude vision.

Claude only transcribes what it sees (dates and the bottom "Total" row, as
text). All parsing and arithmetic happens here in code. Each result is cached
in a sidecar file next to the image ("<image>.timesheet.json") so a screenshot
is only sent to the API once.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel

from invoice_agent.durations import format_duration, parse_duration

MODEL = "claude-sonnet-5-5"
SIDECAR_SUFFIX = ".timesheet.json"
MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

PROMPT = """\
This is a screenshot of a weekly timesheet. Transcribe it exactly; do not do any math.

- date_range_header: the date range shown near the top, e.g. "31 Aug 26 - 06 Sep 26".
- days: one entry per day column (Mon through Sun), in order. For each, give the
  column's date as YYYY-MM-DD (the header uses 2-digit years, so "26" means 2026)
  and the value from the bottom "Total" row for that column, copied exactly as
  shown (e.g. "1h 30m", "45m", "0m").
- weekly_total: the bottom-right grand total, exactly as shown.
- Ignore project names and the individual project rows; only the bottom "Total" row matters.
- confidence: "low" if anything is cut off, blurry, or ambiguous; explain in notes.
"""


class DayCell(BaseModel):
    date: str
    total: str


class Extraction(BaseModel):
    """What Claude returns: a transcription, not a calculation."""

    date_range_header: str
    days: list[DayCell]
    weekly_total: str
    confidence: Literal["high", "medium", "low"]
    notes: str


@dataclass
class TimesheetWeek:
    image: Path
    week_start: date  # Monday
    day_minutes: dict[date, int]  # all 7 days of the week, including other months
    total_minutes: int  # the screenshot's own weekly total
    confidence: str
    notes: str = ""
    problems: list[str] = field(default_factory=list)  # failed consistency checks

    def minutes_in(self, first: date, last: date) -> int:
        """Minutes logged on days between `first` and `last`, inclusive."""
        return sum(m for d, m in self.day_minutes.items() if first <= d <= last)

    @property
    def ok(self) -> bool:
        return not self.problems and self.confidence != "low"


class TimesheetReadError(RuntimeError):
    pass


def to_week(image: Path, extraction: Extraction) -> TimesheetWeek:
    """Turn a raw transcription into minutes per date and run consistency checks."""
    problems: list[str] = []
    day_minutes: dict[date, int] = {}
    for cell in extraction.days:
        try:
            day_minutes[date.fromisoformat(cell.date)] = parse_duration(cell.total)
        except ValueError as err:
            problems.append(f"Unreadable day {cell.date!r} / {cell.total!r}: {err}")

    try:
        total = parse_duration(extraction.weekly_total)
    except ValueError as err:
        problems.append(f"Unreadable weekly total: {err}")
        total = 0

    dates = sorted(day_minutes)
    if not dates:
        raise TimesheetReadError(f"{image.name}: no days could be read")
    week_start = dates[0]
    expected = [week_start + timedelta(days=i) for i in range(7)]
    if week_start.weekday() != 0:
        problems.append(f"First day {week_start} is not a Monday")
    if dates != expected:
        problems.append(f"Expected 7 consecutive days from {week_start}, got {[str(d) for d in dates]}")
    if sum(day_minutes.values()) != total:
        problems.append(
            f"Daily totals add up to {format_duration(sum(day_minutes.values()))} "
            f"but the screenshot's weekly total is {format_duration(total)}"
        )

    return TimesheetWeek(
        image=image,
        week_start=week_start,
        day_minutes=day_minutes,
        total_minutes=total,
        confidence=extraction.confidence,
        notes=extraction.notes,
        problems=problems,
    )


def read_timesheet(
    image: Path, client: anthropic.Anthropic | None = None, refresh: bool = False
) -> TimesheetWeek:
    """Read a screenshot, using the cached sidecar when the image hasn't changed.

    Results with failed checks are returned but not cached, so the next run retries.
    """
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    sidecar = sidecar_path(image)
    if not refresh and sidecar.exists():
        cached = json.loads(sidecar.read_text())
        if cached.get("sha256") == digest:
            return to_week(image, Extraction.model_validate(cached["extraction"]))

    extraction = _extract(image, client or make_client())
    week = to_week(image, extraction)
    if week.ok:
        sidecar.write_text(
            json.dumps({"sha256": digest, "model": MODEL, "extraction": extraction.model_dump()}, indent=2)
        )
    return week


def make_client() -> anthropic.Anthropic:
    """API client from the environment (.env).

    ANTHROPIC_WORKSPACE_ID is only needed when the API key isn't scoped to a workspace.
    """
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    headers = {"anthropic-workspace-id": workspace} if workspace else None
    return anthropic.Anthropic(default_headers=headers)


def sidecar_path(image: Path) -> Path:
    return image.with_name(image.name + SIDECAR_SUFFIX)


def _extract(image: Path, client: anthropic.Anthropic) -> Extraction:
    media_type = MEDIA_TYPES.get(image.suffix.lower())
    if media_type is None:
        raise TimesheetReadError(f"{image.name}: unsupported image type")
    data = base64.standard_b64encode(image.read_bytes()).decode("utf-8")

    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "medium"},
        # If a safety classifier ever declines, the API retries on a fallback model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
        output_format=Extraction,
    )
    if response.stop_reason == "refusal":
        raise TimesheetReadError(f"{image.name}: the model declined to read this image")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise TimesheetReadError(f"{image.name}: incomplete response ({response.stop_reason})")
    return response.parsed_output
