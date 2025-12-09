import os  # gives access to environment variables via os.getenv
from dataclasses import dataclass  # helper that auto-generates boilerplate methods for simple classes
from typing import Optional  # lets us express "this can be a type OR None", e.g. Optional[str]
from dotenv import load_dotenv

load_dotenv()

@dataclass  # transforms the Settings class below into a "data class" with an auto-generated __init__, etc.
class Settings:
    # Telegram
    telegram_bot_token: Optional[str]  # can be a string OR None (if env var not set)
    telegram_user_id: Optional[int]    # can be an integer OR None

    # Google Sheets
    google_sheets_spreadsheet_id: Optional[str]  # string sheet ID or None
    google_service_account_json: Optional[str]   # JSON string for service account or None

    # Gmail
    gmail_user_id: str  # required string; no Optional here, so it's expected to always be a str (e.g. "me")

    # General
    timezone: str  # required string; e.g. "Asia/Singapore"
    webhook_url: str # webhook url


def load_settings() -> Settings:
    """
    Load configuration from environment variables.

    For now we don't enforce that everything is set.
    We'll validate specific fields when we actually need them.
    """
    # get TELEGRAM_USER_ID as a raw string from env; result is either a string or None
    telegram_user_id_raw = os.getenv("TELEGRAM_USER_ID")

    # create and return a Settings object, passing in all the fields
    return Settings(
        # gets the token string or None if the env var is missing
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),

        # if we have a raw value, convert it to int; otherwise set to None
        telegram_user_id=int(telegram_user_id_raw) if telegram_user_id_raw else None,

        # read spreadsheet ID from env or None
        google_sheets_spreadsheet_id=os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID"),

        # read service account JSON (path or contents, depending on your design) or None
        google_service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"),

        # read Gmail user ID; default to "me" if env var is not set
        gmail_user_id=os.getenv("GMAIL_USER_ID", "me"),

        # read timezone; default to "Asia/Singapore" if env var is not set
        timezone=os.getenv("APP_TIMEZONE", "Asia/Singapore"),
        webhook_url=os.getenv("WEBHOOK_URL")
    )