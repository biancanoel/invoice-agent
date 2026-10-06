"""merge_screenshots: combine weekly timesheet screenshots into one PDF.

Screenshot filenames carry the capture time, not the week shown, so every
image must be paired with the Monday of the week it shows. In Phase 1 that
comes from the CLI; later it comes from read_timesheet's extracted dates.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import img2pdf
from PIL import Image

from invoice_agent.weeks import Month, billing_weeks

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


@dataclass(frozen=True)
class Screenshot:
    path: Path
    week_start: date  # Monday of the Monday-Sunday week the screenshot shows

    def overlaps(self, month: Month) -> bool:
        week_end = self.week_start + timedelta(days=6)
        return self.week_start <= month.last_day and week_end >= month.first_day


@dataclass
class MergeResult:
    output: Path
    included: list[Screenshot]
    skipped: list[Screenshot] = field(default_factory=list)  # weeks outside the month
    missing_weeks: list[date] = field(default_factory=list)  # Mondays with no screenshot


class MergeError(ValueError):
    pass


def merge_screenshots(screenshots: list[Screenshot], month: Month, output: Path) -> MergeResult:
    """Write the month's screenshots, earliest week first, into a single PDF.

    Screenshots for weeks that don't touch `month` are skipped. Missing weeks
    are reported but don't stop the merge; duplicate weeks do.
    """
    for shot in screenshots:
        if shot.week_start.weekday() != 0:
            raise MergeError(f"{shot.path.name}: week start {shot.week_start} is not a Monday")

    in_month = sorted((s for s in screenshots if s.overlaps(month)), key=lambda s: s.week_start)
    skipped = [s for s in screenshots if not s.overlaps(month)]

    seen: dict[date, Screenshot] = {}
    for shot in in_month:
        if shot.week_start in seen:
            raise MergeError(
                f"Two screenshots for the week of {shot.week_start}: "
                f"{seen[shot.week_start].path.name} and {shot.path.name}"
            )
        seen[shot.week_start] = shot

    if not in_month:
        raise MergeError(f"No screenshots cover {month.first_day:%B %Y}")

    missing = [w.week_start for w in billing_weeks(month) if w.week_start not in seen]

    pages = [_to_png_bytes(s.path) for s in in_month]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(img2pdf.convert(pages))

    return MergeResult(output=output, included=in_month, skipped=skipped, missing_weeks=missing)


def _to_png_bytes(path: Path) -> bytes:
    """Load an image, flatten any transparency onto white, and return lossless PNG bytes.

    img2pdf refuses images with an alpha channel, and macOS screenshots often have one.
    """
    with Image.open(path) as img:
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            flat = Image.new("RGB", rgba.size, "white")
            flat.paste(rgba, mask=rgba.getchannel("A"))
        else:
            flat = img.convert("RGB")
        buf = io.BytesIO()
        flat.save(buf, format="PNG")
        return buf.getvalue()


def find_images(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
