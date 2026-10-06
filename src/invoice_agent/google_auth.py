"""Google sign-in for the Drive and Sheets APIs.

The first run opens a browser to approve access; the result is saved to
token.json so later runs don't ask again (until Google expires it).
"""

from __future__ import annotations

from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Least privilege: read your Drive (to copy the template and check for an existing
# invoice), and edit only files this app creates (the monthly copies).
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]


def get_credentials(credentials_file: Path, token_file: Path) -> Credentials:
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            creds = None  # refresh token expired or revoked; sign in again
    if not creds or not creds.valid:
        if not credentials_file.exists():
            raise FileNotFoundError(
                f"{credentials_file} not found. Download the OAuth client file from Google Cloud "
                "(see README: Google setup)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
        creds = flow.run_local_server(port=0)

    token_file.write_text(creds.to_json())
    return creds
