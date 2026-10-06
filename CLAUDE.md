# Invoice Agent

Turns a month of weekly timesheet screenshots into a client invoice (Google Sheet + PDF) and a merged timesheet PDF. Phase 1 is a CLI (`invoice-agent`); Phase 2 is a chat agent on the Claude Agent SDK (`invoice-agent agent`). See README.md for usage and setup.

## Commands

- `uv run pytest`: run all tests. They're offline: the Claude API, Google APIs, and user input are faked.
- `uv run invoice-agent <command>`: run the CLI (`weeks`, `read`, `verify`, `merge`, `invoice`, `agent`).
- `uv add <pkg>` / `uv add --dev <pkg>`: add dependencies. Don't use pip.

## Layout (`src/invoice_agent/`)

- `weeks.py`: billing weeks. Monday to Sunday, clipped to the month; labels like "September 1-6".
- `durations.py`: **all** conversion between "1h 30m" text, minutes and decimal hours. Nothing else multiplies or divides by 60.
- `timesheet.py`: reads a screenshot with Claude vision and caches the result as `<image>.timesheet.json`.
- `verify.py`: compares typed weekly totals to the screenshots. Pure Python.
- `merge.py`: builds the timesheet PDF.
- `invoice.py`: `plan_invoice` (pure) and `create_invoice` (through the `Workspace` protocol).
- `google_auth.py` / `google_workspace.py`: Google sign-in and the real Drive/Sheets calls.
- `report.py`: text output shared by the CLI and the agent.
- `agent.py` + `prompts/system.md`: the Agent SDK tools, the approval gate, and the agent's instructions.
- `cli.py`: argument parsing and printing only. Logic belongs in the modules above.

## Rules that matter

- **Claude transcribes, code calculates.** The vision call returns text exactly as shown on the screenshot. Parsing, sums and month clipping happen in Python. Don't move arithmetic into prompts.
- **Time is whole minutes internally.** Convert to decimal hours (`to_hours`, 2 decimals) only for the invoice.
- **Every week's typed entry is the screenshot's full weekly total** (bottom-right). The tools work out the in-month share.
- **The `create_invoice` approval is enforced in code, not the prompt.** In the agent:
  - `create_invoice` must never be in `allowed_tools`.
  - `permission_mode` stays `"default"`.
  - The tool must keep refusing unless `ApprovalGate` holds an approval for that exact request.
  - The CLI asks y/N before creating.
- **Agent sandbox:** `tools=[]` (no built-in tools) and `setting_sources=[]`. The tools only touch folders inside `data/`.
- **The template layout lives in config** (`Layout` in `config.py`, `[layout]` in `config.toml`). The current template has 6 line-item rows (20-25). Writing past them would overwrite the Notes and Subtotal rows.
- **Write RAW values to Sheets** so "09-2026" isn't turned into a date. Dates go in as serial numbers.

## Secrets and client data (public repo)

- **Never commit** `.env`, `config.toml`, `credentials.json`, `token.json`, or anything in `data/`. All of these are gitignored; keep it that way.
- **No real client names or IDs** in committed code, tests or docs. Use generic names ("Invoice September 2026") and fake IDs in tests.

## Style

- Match the surrounding code: small modules, dataclasses, docstrings that explain why, few comments.
- Every behavior change gets a test. Fake the Claude API, Google and `input()` the same way the existing tests do.
- Model IDs: `claude-sonnet-5-5` for screenshot reading and the agent. Pass the exact ID strings, with no date suffixes.
