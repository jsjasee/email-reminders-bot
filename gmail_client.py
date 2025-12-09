import json
from typing import Any, Dict, List

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# We requested this scope in gmail_oauth_setup.py
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClient:
    """
    Gmail API client using user OAuth (gmail_token.json contents).

    Assumptions:
      - GMAIL_OAUTH_TOKEN_JSON contains the JSON written by creds.to_json()
        from google-auth-oauthlib's InstalledAppFlow.
      - That JSON includes a refresh_token so access tokens can be refreshed.
    """

    def __init__(self, oauth_token_json: str) -> None:
        if not oauth_token_json:
            raise ValueError("GMAIL_OAUTH_TOKEN_JSON is empty; cannot init GmailClient.")

        try:
            token_info: Dict[str, Any] = json.loads(oauth_token_json)
        except json.JSONDecodeError as e:
            raise ValueError("GMAIL_OAUTH_TOKEN_JSON is not valid JSON.") from e

        # Build Credentials from the saved token info.
        # Passing scopes ensures they’re set even if missing in the JSON.
        creds = Credentials.from_authorized_user_info(token_info, scopes=GMAIL_SCOPES)

        self.service = build(
            "gmail",
            "v1",
            credentials=creds,
            cache_discovery=False,
        )

    def list_labels(self) -> List[Dict[str, Any]]:
        """Return the list of labels for the authorised user."""
        try:
            resp = self.service.users().labels().list(userId="me").execute()
            return resp.get("labels", [])
        except HttpError as e:
            # Keep logs minimal; no sensitive data.
            print(f"Gmail API error in list_labels: {e}")
            raise