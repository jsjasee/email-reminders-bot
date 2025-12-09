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

    def list_recent_message_ids(
        self,
        max_results: int = 10,
        label_ids: list[str] | None = None,
        query: str | None = None,
    ) -> list[str]:
        """
        Return a list of recent Gmail message IDs.

        This is a simple helper for testing; later we'll use history IDs
        from Pub/Sub to be more precise.
        """
        kwargs: dict[str, Any] = {
            "userId": "me",
            "maxResults": max_results,
        }
        if label_ids:
            kwargs["labelIds"] = label_ids
        if query:
            kwargs["q"] = query

        try:
            resp = self.service.users().messages().list(**kwargs).execute()
        except HttpError as e:
            print(f"Gmail API error in list_recent_message_ids: {e}")
            raise

        messages = resp.get("messages", [])
        return [m["id"] for m in messages]

    def get_message_metadata(self, message_id: str) -> Dict[str, Any]:
        """
        Fetch minimal metadata for a specific message:
        - gmail_message_id
        - from
        - to
        - subject
        - internal_date (ms since epoch as string)
        - label_ids
        """
        try:
            resp = (
                self.service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "To", "Subject"],
                )
                .execute()
            )
        except HttpError as e:
            print(f"Gmail API error in get_message_metadata: {e}")
            raise

        headers = resp.get("payload", {}).get("headers", []) # headers = email envelope fields: From, To, Subject.
        header_map = {h.get("name"): h.get("value") for h in headers} # headers_map is just a convenience to convert
        # these list of headers from gmail to convert it into a readable dict.

        return {
            "gmail_message_id": resp.get("id"),
            "from": header_map.get("From"),
            "to": header_map.get("To"),
            "subject": header_map.get("Subject"),
            "internal_date": resp.get("internalDate"),
            "label_ids": resp.get("labelIds", []),
        }