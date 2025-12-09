import logging

from flask import Flask, jsonify, request

from config import load_settings, Settings
from telegram_bot import TelegramBot
from sheets_repo import ReminderSheetRepository

# will show errors in the console with different levels - levels are just priority labels for these messages,
# like DEBUG, INFO, WARNING etc. (similar to roblox studio)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__)

    # Load config once at startup
    settings: Settings = load_settings()
    app.config["SETTINGS"] = settings

    # Initialise TelegramBot (may be None if token is missing)
    telegram_bot = None
    if settings.telegram_bot_token:
        telegram_bot = TelegramBot(
            token=settings.telegram_bot_token,
            allowed_user_id=settings.telegram_user_id,
        )
    app.config["TELEGRAM_BOT"] = telegram_bot

    # Initialise Sheets repo (may be None if config missing / invalid)
    sheets_repo = None
    if settings.google_sheets_spreadsheet_id and settings.google_service_account_json:
        try:
            sheets_repo = ReminderSheetRepository(
                spreadsheet_id=settings.google_sheets_spreadsheet_id,
                service_account_json=settings.google_service_account_json,
            )
            logger.info("Initialised ReminderSheetRepository successfully.")
        except Exception:
            logger.exception("Failed to initialise ReminderSheetRepository.")
            sheets_repo = None
    else:
        logger.warning("Google Sheets config missing; repo not initialised.")

    app.config["REMINDER_REPO"] = sheets_repo

    @app.route("/", methods=["GET"])
    def index():
        return "Email → Telegram reminder bot is running", 200

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify(
            {
                "status": "ok",
                "timezone": settings.timezone,
                "telegram_configured": settings.telegram_bot_token is not None,
                "sheets_configured": sheets_repo is not None,
            }
        ), 200

    @app.route("/telegram-webhook", methods=["POST"])
    def telegram_webhook():
        """
        Minimal handler:
        - Accepts Telegram update JSON
        - Enforces single-user rule
        - Responds to /start with a simple message
        """
        bot: TelegramBot | None = app.config.get("TELEGRAM_BOT")
        if bot is None:
            logger.error("Telegram bot not configured (missing TELEGRAM_BOT_TOKEN)")
            return "Telegram bot not configured", 500

        update = request.get_json(silent=True) or {}
        logger.info("Received Telegram update: %s", update)

        if not bot.is_allowed_user(update):
            logger.warning("Update from disallowed user, ignoring.")
            # Return 200 so Telegram doesn't keep retrying
            return "", 200

        message = update.get("message")
        if message:
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            text = message.get("text") or ""

            if text == "/start":
                bot.send_message(
                    chat_id=chat_id,
                    text="Hello! This is your Email → Telegram reminder bot.",
                )

        # For now, ignore other update types.
        return "", 200

    @app.route("/test-sheets", methods=["POST", "GET"])
    def test_sheets():
        """
        Simple connectivity test:
        - Append a dummy row
        - Return total rows and last row
        """
        repo: ReminderSheetRepository | None = app.config.get("REMINDER_REPO")
        if repo is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Sheets repo not configured. Check GOOGLE_SHEETS_SPREADSHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON.",
                    }
                ),
                500,
            )

        try:
            repo.append_test_row("test from /test-sheets")
            values = repo.get_all_values()
            last_row = values[-1] if values else None
            return jsonify(
                {
                    "ok": True,
                    "total_rows": len(values),
                    "last_row": last_row,
                }
            )
        except Exception as e:
            logger.exception("Error during /test-sheets")
            return jsonify({"ok": False, "error": str(e)}), 500

    return app


app = create_app()

if __name__ == "__main__":
    # For local development only. On Render we'll use gunicorn.
    app.run(host="0.0.0.0", port=5001, debug=True)