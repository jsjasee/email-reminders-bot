"""
One-time Gmail OAuth setup script for a personal @gmail.com account.

Usage:
1. pip install --upgrade google-auth google-auth-oauthlib google-api-python-client
2. python gmail_oauth_setup.py
3. When prompted, enter the path to the downloaded OAuth client JSON
   (the Desktop app client you created in Google Cloud Console).
4. A browser window will open; log in as your inbox B and grant access.
5. The script will create gmail_token.json in the current directory.
"""

import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

# For now we only need read access.
# If we later need more (e.g. modify labels), we can add scopes and re-run.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main() -> None:
    print("=== Gmail OAuth setup ===")
    print(
        "This will use your downloaded OAuth client JSON (Desktop app) "
        "to generate a gmail_token.json with a refresh token."
    )
    raw_path = input(
        "Enter the full path to your OAuth client JSON file "
        "(or drag-drop it here and press Enter):\n> "
    ).strip()

    # Handle drag-drop with quotes/spaces
    raw_path = raw_path.strip('"').strip("'")
    client_secrets_path = Path(raw_path).expanduser().resolve()

    if not client_secrets_path.is_file():
        raise FileNotFoundError(
            f"Client secrets file not found at: {client_secrets_path}"
        )

    print(f"\nUsing client secrets file: {client_secrets_path}")

    # Run the local server flow: opens a browser window for you to consent.
    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_secrets_path),
        SCOPES,
    )

    print("\nOpening browser for Google consent flow...")
    creds = flow.run_local_server(
        port=0,  # choose any free port
        prompt="consent",
        authorization_prompt_message=(
            "Please authorize access to Gmail for this app.\n\n"
            "After approving, you can close the browser tab and return here.\n"
        ),
    )

    token_path = Path("creds/gmail_token.json").resolve()
    token_path.write_text(creds.to_json(), encoding="utf-8")

    print("\n=== Success ===")
    print(f"Token saved to: {token_path}")
    print(
        "\nNext steps (don’t do yet, just FYI):\n"
        " - You will later copy the *entire* contents of this gmail_token.json\n"
        "   into an environment variable (GMAIL_OAUTH_TOKEN_JSON) on Render.\n"
        " - You will also copy the *entire* contents of your client JSON file\n"
        "   into another environment variable (GMAIL_OAUTH_CLIENT_CONFIG_JSON).\n"
    )


if __name__ == "__main__":
    main()