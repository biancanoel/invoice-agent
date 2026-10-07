"""Draw fake weekly timesheet screenshots for the demo (examples/timesheets/).

The layout mimics a typical timesheet app: one row per task, a column per
day Monday to Sunday, and a bottom "Total" row with each day's total and the
weekly total in the bottom-right. Every project name and hour is made up.

    uv run python scripts/make_demo_screenshots.py

Prints the weekly totals to type into `invoice-agent verify` or the agent.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from invoice_agent.durations import format_duration
from invoice_agent.weeks import Month, billing_weeks

MONTH = Month.parse("2026-04")
OUT = Path(__file__).resolve().parent.parent / "examples" / "timesheets"
TASKS = [
    ("Example Co. - Website", "Homepage Redesign"),
    ("Example Co. - Website", "Content Migration"),
    ("Example Co. - Training", "Admin Workshop"),
    ("Example Co. - Training", "Office Hours"),
    ("Internal", "Project Review"),
]
STEPS = [0, 0, 0, 15, 20, 30, 45, 60, 75, 90, 120, 150]  # minutes, weighted toward empty cells

WIDTH, HEIGHT = 1700, 760
DAY_X = [700 + 110 * i for i in range(7)]
TOTAL_X = DAY_X[-1] + 120
INK, MUTED, RULE, BAND = (32, 33, 36), (110, 112, 118), (226, 228, 232), (233, 235, 239)


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def week_minutes(rng: random.Random, monday: date) -> list[list[int]]:
    """Minutes per task per day; weekends stay empty."""
    return [[rng.choice(STEPS) if i < 5 else 0 for i in range(7)] for _ in TASKS]


def draw_week(monday: date, minutes: list[list[int]]) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), "white")
    d = ImageDraw.Draw(img)
    sunday = monday + timedelta(days=6)

    d.text((40, 28), "Timesheets", font=font(30), fill=INK)
    d.text((40, 82), "My timesheet", font=font(20), fill=INK)
    d.line((40, 112, 220, 112), fill=INK, width=3)
    d.line((0, 120, WIDTH, 120), fill=RULE, width=1)
    d.text((40, 145), f"{monday:%d %b %y} - {sunday:%d %b %y}", font=font(22), fill=INK)

    header_y = 210
    d.text((40, header_y), "Projects", font=font(20), fill=INK)
    d.text((360, header_y), "Work", font=font(20), fill=INK)
    for i, x in enumerate(DAY_X):
        day = monday + timedelta(days=i)
        d.text((x, header_y - 12), f"{day:%a}", font=font(20), fill=INK)
        d.text((x, header_y + 14), f"{day:%d %b}", font=font(16), fill=MUTED)
    d.text((TOTAL_X, header_y), "Total", font=font(20), fill=INK)
    d.line((40, header_y + 48, WIDTH - 40, header_y + 48), fill=RULE, width=2)

    y = header_y + 70
    for (project, work), row in zip(TASKS, minutes):
        d.text((40, y), project, font=font(19), fill=INK)
        d.text((360, y), work, font=font(19), fill=INK)
        for x, m in zip(DAY_X, row):
            d.text((x, y), format_duration(m) if m else "-", font=font(19), fill=INK if m else MUTED)
        d.text((TOTAL_X, y), format_duration(sum(row)), font=font(19), fill=INK)
        d.line((40, y + 40, WIDTH - 40, y + 40), fill=RULE, width=1)
        y += 62

    band_y = HEIGHT - 90
    d.rectangle((40, band_y, WIDTH - 40, band_y + 56), fill=BAND)
    d.text((60, band_y + 16), "Total", font=font(20), fill=INK)
    for i, x in enumerate(DAY_X):
        d.text((x, band_y + 16), format_duration(sum(r[i] for r in minutes)), font=font(20), fill=INK)
    d.text((TOTAL_X, band_y + 16), format_duration(sum(map(sum, minutes))), font=font(20), fill=INK)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rng = random.Random(2026)
    totals = []
    for n, week in enumerate(billing_weeks(MONTH)):
        minutes = week_minutes(rng, week.week_start)
        # Named like macOS screenshots: capture times, deliberately not in week order.
        taken = date(2026, 5, 1) + timedelta(days=(n * 3) % 5)
        hour = 9 + n
        name = f"Screenshot {taken} at {(hour - 1) % 12 + 1}.{15 * (n % 4):02d}.00 {'AM' if hour < 12 else 'PM'}.png"
        draw_week(week.week_start, minutes).save(OUT / name)
        totals.append(format_duration(sum(map(sum, minutes))))
    print(f"Wrote {len(totals)} screenshots to {OUT}")
    print(f'Weekly totals: "{", ".join(totals)}"')


if __name__ == "__main__":
    main()
