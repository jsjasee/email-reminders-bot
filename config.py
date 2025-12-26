import logging
import os  # gives access to environment variables via os.getenv
from dataclasses import dataclass  # helper that auto-generates boilerplate methods for simple classes
from typing import Optional  # lets us express "this can be a type OR None", e.g. Optional[str]
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def parse_allowed_sender_emails(raw: Optional[str]) -> list[str]:
    """
    Parse a simple newline-delimited list of emails into a cleaned allowlist.

    Expected format (example for ALLOWED_SENDER_EMAILS):

        abc@gmail.com
        def@example.com
        ghi@domain.org

    - One email per line
    - No comments
    - No "Name <email>" format
    """

    if not raw:
        # If the input string is empty or None, return an empty list
        return []

    # Normalise line endings: convert Windows (\r\n) / old Mac (\r) to Unix (\n)
    normalised = raw.replace("\r\n", "\n").replace("\r", "\n")

    allowed: list[str] = []  # Final list of cleaned email addresses
    seen: set[str] = set()   # Set used to avoid duplicates

    # Split the string into individual lines (one potential email per line)
    for line in normalised.split("\n"):
        # Remove leading and trailing spaces from the line
        email = line.strip()

        # Skip completely empty lines
        if not email:
            continue

        # Normalise to lowercase so comparisons are case-insensitive
        email = email.casefold()

        # Very simple validation: must contain '@' and no spaces
        if "@" not in email or " " in email:
            # Log and skip anything that doesn't look like a basic email
            logger.warning(
                "Ignoring invalid sender address in ALLOWED_SENDER_EMAILS: %s",
                line,
            )
            continue

        # Only add the email if we have not seen it before
        if email not in seen:
            allowed.append(email)  # Add to final list
            seen.add(email)        # Mark as seen to prevent duplicates

    # If after parsing everything we ended up with no valid emails, treat it as misconfig
    if not allowed:
        raise ValueError("ALLOWED_SENDER_EMAILS produced an empty allowlist")

    # Return the cleaned list of allowed sender email addresses
    return allowed

@dataclass  # transforms the Settings class below into a "data class" with an auto-generated __init__, etc.
@dataclass
class Settings:
    # Telegram
    telegram_bot_token: Optional[str]
    telegram_user_id: Optional[int]

    # Google Sheets
    google_sheets_spreadsheet_id: Optional[str]
    google_service_account_json: Optional[str]

    # Gmail
    gmail_user_id: str  # keep this; may be useful later
    # NEW: raw JSON string from gmail_token.json
    gmail_oauth_token_json: Optional[str]

    # General
    timezone: str
    webhook_url: str
    target_sender_email: Optional[str]
    allowed_sender_emails: list[str]

def load_settings() -> Settings:
    telegram_user_id_raw = os.getenv("TELEGRAM_USER_ID")

    raw_allowed = os.getenv("ALLOWED_SENDER_EMAILS")
    allowed_emails: list[str] = []
    target_sender_email = os.getenv("TARGET_SENDER_EMAIL")

    if raw_allowed:
        allowed_emails = parse_allowed_sender_emails(raw_allowed)

    if not allowed_emails and target_sender_email:
        logger.warning(
            "TARGET_SENDER_EMAIL is deprecated; please migrate to ALLOWED_SENDER_EMAILS"
        )
        allowed_emails = parse_allowed_sender_emails(target_sender_email)

    if not allowed_emails:
        raise ValueError("No allowed sender emails configured")

    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        telegram_user_id=int(telegram_user_id_raw) if telegram_user_id_raw else None,
        google_sheets_spreadsheet_id=os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID"),
        google_service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"),
        gmail_user_id=os.getenv("GMAIL_USER_ID", "me"),
        gmail_oauth_token_json=os.getenv("GMAIL_OAUTH_TOKEN_JSON"),
        timezone=os.getenv("APP_TIMEZONE", "Asia/Singapore"),
        webhook_url=os.getenv("WEBHOOK_URL"),
        target_sender_email=target_sender_email,
        allowed_sender_emails=allowed_emails,
    )
