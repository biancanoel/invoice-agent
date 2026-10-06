# Invoice Agent

Builds the monthly client invoice and a single PDF of weekly timesheet screenshots.

## Status

| Phase | Piece | State |
|---|---|---|
| 1 | `merge_screenshots`: sort weekly screenshots and combine them into one PDF | done |
| 1 | `read_timesheet`: one vision call per screenshot that returns the week and daily hours | done |
| 1 | `verify_totals`: plain Python, checks typed weekly totals and works out each week's in-month time | done |
| 1 | `create_invoice`: copy the Google Sheet template, fill rows, export PDF | built; needs Google setup to run |
| 2 | Agent built with the Claude Agent SDK (custom tools only, built-in tools off) | |
| 3 | Approval gate on `create_invoice` (CLI has dry run + y/N prompt), demo data | |

## Rules

- **Weeks** run Monday to Sunday, matching the timesheet tool. Invoice line items are those weeks clipped to the month, e.g. September 2026 is `September 1-6, 7-13, 14-20, 21-27, 28-30`.
- A boundary week (e.g. Aug 31 to Sep 6) is included in **both** months' PDFs. Only the in-month days count toward each invoice.
- **Invoice number** is `MM-YYYY`, e.g. `09-2026`.
- Hours on the invoice are decimals. One fixed rate, one client.
- **Submitted on** and **Due date** are both the last day of the month.
- Screenshot filenames give the capture time, not the week shown, so they're never used for ordering.

## Setup

```bash
uv sync
cp .env.example .env   # then paste your Anthropic API key into .env
```

## Usage

```bash
uv run invoice-agent weeks --month 2026-09            # invoice line items for the month
uv run invoice-agent read data/2026-09 --month 2026-09 # what Claude read from each screenshot
uv run invoice-agent merge data/2026-09 --month 2026-09
uv run invoice-agent verify data/2026-09 --month 2026-09 --hours "2h 30m, 10h, 5h 30m, 7h 30m, 15h 20m"
```

`verify` takes one entry per week (run `weeks` to see them), earliest first: the **weekly total from the bottom-right of each screenshot**, typed as shown (`40m`, `1h 30m`, `15h 20m`). Decimal hours like `1.5` also work. Each entry is compared to its screenshot in exact minutes. For weeks that cross into another month, the invoice amount is worked out from the screenshot's daily totals, so you never add anything up. The output shows each week's in-month time, the decimal hours for the invoice, and the invoice total. It flags mismatches, missing or duplicate screenshots, and readings that failed their checks.

`read` and `merge` send each screenshot to Claude **once**. The result is saved next to the image as `<image>.timesheet.json`, and later runs reuse it unless the image changes. Use `read --refresh` to force a re-read.

Claude only copies text off the screenshot (the dates and the bottom **Total** row). The code converts durations, adds up hours and checks that:
- the 7 days run Monday to Sunday with no gaps, and
- the daily totals add up to the screenshot's weekly total.

A result that fails a check is shown as a `PROBLEM` and isn't saved, so it's re-read next time.

To merge without calling the API, use `merge --manual` (it asks for the week of each image) or `--weeks 2026-08-31,2026-09-07,...`.

### Creating the invoice

```bash
uv run invoice-agent invoice data/2026-09 --month 2026-09 --hours "1h 40m, 10h, 9h, 0m, 7h 20m" --dry-run
uv run invoice-agent invoice data/2026-09 --month 2026-09 --hours "1h 40m, 10h, 9h, 0m, 7h 20m"
```

`invoice` runs the same check as `verify` and stops if anything is flagged. It then shows exactly what will be written to each cell. With `--dry-run` it stops there. Without it, it asks `Create ...? [y/N]` and only then:

1. Checks that this month's invoice doesn't already exist in the folder. If it does, it refuses to overwrite it.
2. Copies the template into the folder as e.g. `Invoice September 2026`.
3. Writes the submitted date, invoice number, due date, and each week's description and hours. Unused rows are cleared. The rate and all amounts come from the template's own formulas.
4. Saves the PDF as `data/2026-09/Invoice 09-2026.pdf`.

## Google setup (one time)

1. **Make a dedicated template.** In Drive, open an existing invoice and choose **File > Make a copy**. Name it e.g. `Invoice Template` and don't use it for a real month.
2. **Create a Google Cloud project** at console.cloud.google.com (e.g. "Invoice Agent").
3. **Turn on the APIs.** Under **APIs & Services > Library**, enable **Google Drive API** and **Google Sheets API**.
4. **Set up the consent screen.** Under **Google Auth Platform**, choose audience **External** and add your own Google account as a **test user**.
5. **Create the client.** Under **Clients**, create a **Desktop app** client and download its JSON file. Save it in this folder as `credentials.json`. It's gitignored.
6. **Fill in the config.** Copy `config.example.toml` to `config.toml` and set `template_id` (from the template's URL) and `folder_id` (from the Drive folder's URL).

The first real run opens a browser to approve access. You'll see a "Google hasn't verified this app" warning because it's your own app; continue past it. The tool asks only for **read-only** access to Drive plus access to **files it creates**, so it can copy your template but can't change or delete anything else. While the app is in testing mode, Google expires the sign-in after 7 days, so expect the browser approval each month.

## Tests

```bash
uv run pytest
```
