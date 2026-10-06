"""The real Google Drive/Sheets calls behind invoice.Workspace."""

from __future__ import annotations

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# Export options for the PDF: letter size, portrait, fit to page width, no gridlines.
PDF_OPTIONS = {
    "format": "pdf",
    "size": "letter",
    "portrait": "true",
    "fitw": "true",
    "gridlines": "false",
    "printtitle": "false",
    "sheetnames": "false",
    "pagenum": "UNDEFINED",
}


class GoogleWorkspace:
    def __init__(self, creds: Credentials):
        self._drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        self._sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self._session = AuthorizedSession(creds)

    def find_file(self, name: str, folder_id: str) -> str | None:
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name = '{escaped}' and '{folder_id}' in parents and trashed = false"
        files = self._drive.files().list(q=query, fields="files(id)", pageSize=1).execute().get("files", [])
        return files[0]["id"] if files else None

    def copy_file(self, file_id: str, name: str, folder_id: str) -> str:
        body = {"name": name, "parents": [folder_id]}
        return self._drive.files().copy(fileId=file_id, body=body, fields="id").execute()["id"]

    def write_cells(self, spreadsheet_id: str, sheet: str, cells: list[tuple[str, str | float | int]]) -> None:
        data = [{"range": f"'{sheet}'!{cell}", "values": [[value]]} for cell, value in cells]
        body = {"valueInputOption": "RAW", "data": data}
        self._sheets.spreadsheets().values().batchUpdate(spreadsheetId=spreadsheet_id, body=body).execute()

    def export_pdf(self, spreadsheet_id: str, sheet: str) -> bytes:
        params = {**PDF_OPTIONS, "gid": str(self._sheet_gid(spreadsheet_id, sheet))}
        url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export"
        response = self._session.get(url, params=params)
        response.raise_for_status()
        if not response.content.startswith(b"%PDF"):
            raise RuntimeError("Google returned something other than a PDF")
        return response.content

    def _sheet_gid(self, spreadsheet_id: str, sheet: str) -> int:
        meta = self._sheets.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties").execute()
        for s in meta["sheets"]:
            if s["properties"]["title"] == sheet:
                return s["properties"]["sheetId"]
        raise RuntimeError(f"No sheet named '{sheet}' in the invoice")
