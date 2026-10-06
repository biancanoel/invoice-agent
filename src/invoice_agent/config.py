"""Settings from config.toml (gitignored; see config.example.toml).

The template's cell layout has defaults matching the current invoice
template, so config.toml only needs the Drive IDs unless the template moves
things around.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_PATH = Path("config.toml")


@dataclass(frozen=True)
class Layout:
    """Where things live in the invoice template."""

    sheet: str = "Invoice"
    submitted_on: str = "B9"  # text: "Submitted on 9/30/2026"
    invoice_number: str = "F12"
    due_date: str = "F16"
    first_line_row: int = 20
    line_rows: int = 6  # rows 20-25; a month never spans more than 6 Monday-Sunday weeks
    description_column: str = "B"
    hours_column: str = "E"


@dataclass(frozen=True)
class Config:
    template_id: str  # the Google Sheet copied each month
    folder_id: str  # Drive folder the new invoices go in
    name_format: str = "Invoice {month_name} {year}"
    credentials_file: Path = Path("credentials.json")  # OAuth client from Google Cloud
    token_file: Path = Path("token.json")  # saved sign-in, created on first run
    layout: Layout = field(default_factory=Layout)


def load_config(path: Path = DEFAULT_PATH) -> Config:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Copy config.example.toml to {path} and fill in the IDs.")
    data = tomllib.loads(path.read_text())
    layout = Layout(**_known(Layout, data.pop("layout", {})))
    values = _known(Config, data)
    for key in ("credentials_file", "token_file"):
        if key in values:
            values[key] = Path(values[key])
    try:
        return Config(**values, layout=layout)
    except TypeError as err:
        raise ValueError(f"{path}: {err}") from None


def _known(cls, data: dict) -> dict:
    names = {f.name for f in fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"Unknown setting(s) in config: {', '.join(sorted(unknown))}")
    return data
