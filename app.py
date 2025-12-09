import logging

from flask import Flask, jsonify, request

from config import load_settings, Settings
from telegram_bot import TelegramBot

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

    return app


app = create_app()

if __name__ == "__main__":
    # For local development only. On Render we'll use gunicorn.
    app.run(host="0.0.0.0", port=5001, debug=True)