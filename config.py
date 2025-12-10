import os  # gives access to environment variables via os.getenv
from dataclasses import dataclass  # helper that auto-generates boilerplate methods for simple classes
from typing import Optional  # lets us express "this can be a type OR None", e.g. Optional[str]
from dotenv import load_dotenv

load_dotenv()

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
    target_sender_email: str

def load_settings() -> Settings:
    telegram_user_id_raw = os.getenv("TELEGRAM_USER_ID")

    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        telegram_user_id=int(telegram_user_id_raw) if telegram_user_id_raw else None,
        google_sheets_spreadsheet_id=os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID"),
        google_service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"),

        gmail_user_id=os.getenv("GMAIL_USER_ID", "me"),

        # NEW:
        gmail_oauth_token_json=os.getenv("GMAIL_OAUTH_TOKEN_JSON"),

        timezone=os.getenv("APP_TIMEZONE", "Asia/Singapore"),
        webhook_url=os.getenv("WEBHOOK_URL"),
        target_sender_email=os.getenv("TARGET_SENDER_EMAIL")
    )