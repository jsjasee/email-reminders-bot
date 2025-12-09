import logging
import uuid

from flask import Flask, jsonify, request

from config import load_settings, Settings
from telegram_bot import TelegramBot
from sheets_repo import ReminderSheetRepository, Reminder
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional
from gmail_client import GmailClient

# In-memory per-chat state for /new manual reminders
manual_new_state: Dict[int, Dict[str, Any]] = {}

# will show errors in the console with different levels - levels are just priority labels for these messages,
# like DEBUG, INFO, WARNING etc. (similar to roblox studio)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def create_app() -> Flask:
    app = Flask(__name__) # this is the flask app, storing routes, requests and configuration settings
    # and also sending responses etc.

    # Load config once at startup
    settings: Settings = load_settings()
    app.config["SETTINGS"] = settings

    # Initialise TelegramBot (maybe None if token is missing)
    telegram_bot = None
    if settings.telegram_bot_token:
        telegram_bot = TelegramBot(
            token=settings.telegram_bot_token,
            allowed_user_id=settings.telegram_user_id,
        )
        telegram_bot.bot.remove_webhook()
        telegram_bot.bot.set_webhook(url=settings.webhook_url)
    app.config["TELEGRAM_BOT"] = telegram_bot

    # Initialise Sheets repo (maybe None if config missing / invalid)
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

    # Initialise Gmail client (OAuth-based, using gmail_token.json contents)
    gmail_client = None
    if settings.gmail_oauth_token_json:
        try:
            gmail_client = GmailClient(settings.gmail_oauth_token_json)
            logger.info("Initialised GmailClient successfully.")
        except Exception:
            logger.exception("Failed to initialise GmailClient.")
            gmail_client = None
    else:
        logger.warning("GMAIL_OAUTH_TOKEN_JSON missing; GmailClient not initialised.")

    app.config["GMAIL_CLIENT"] = gmail_client

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
        Telegram webhook handler.

        Current behaviour:
        - Enforces single-user rule.
        - /start: simple welcome message.
        - /new: ask for reminder description.
        - Next plain-text message after /new: treat as description, show offset buttons.
        - Button press (manual_offset:...): create manual reminder in Sheets.
        """
        bot: TelegramBot | None = app.config.get("TELEGRAM_BOT")
        if bot is None:
            logger.error("Telegram bot not configured (missing TELEGRAM_BOT_TOKEN)")
            return "Telegram bot not configured", 500

        repo: ReminderSheetRepository | None = app.config.get("REMINDER_REPO")

        update = request.get_json(silent=True) or {}
        logger.info("Received Telegram update: %s", update)

        if not bot.is_allowed_user(update):
            logger.warning("Update from disallowed user, ignoring.")
            # Return 200 so Telegram doesn't keep retrying
            return "", 200

        # ------- Helper: parse offset key -> timedelta ------- #

        def offset_key_to_delta(key: str) -> Optional[timedelta]:
            if key == "1h":
                return timedelta(hours=1)
            if key == "1d":
                return timedelta(days=1)
            if key == "3d":
                return timedelta(days=3)
            if key == "1w":
                return timedelta(weeks=1)
            return None

        # ------- Handle normal messages (commands / description) ------- #

        message = update.get("message")
        if message:
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            text = (message.get("text") or "").strip()

            if chat_id is None:
                return "", 200

            # 1) /start
            if text == "/start":
                bot.send_message(
                    chat_id=chat_id,
                    text="Hello! This is your Email → Telegram reminder bot.",
                )
                return "", 200

            # 2) /new: start manual reminder flow
            if text == "/new":
                manual_new_state[chat_id] = {"stage": "awaiting_description"}
                bot.send_message(
                    chat_id=chat_id,
                    text="What should I remind you about?",
                )
                return "", 200

            # 3) If we are expecting a description after /new
            state = manual_new_state.get(chat_id)
            if state and state.get("stage") == "awaiting_description":
                description = text
                if not description:
                    bot.send_message(
                        chat_id=chat_id,
                        text="Please send a short description for the reminder.",
                    )
                    return "", 200

                # Save description, move to next stage
                manual_new_state[chat_id] = {
                    "stage": "awaiting_offset",
                    "description": description,
                }

                # Show buttons for +1h/+1d/+3d/+1w
                keyboard = bot.build_manual_offset_keyboard()
                bot.send_message(
                    chat_id=chat_id,
                    text="When should I remind you?",
                    reply_markup=keyboard,
                )
                return "", 200

            # Any other message: ignore for now
            return "", 200

        # ------- Handle callback queries (button presses) ------- #

        callback_query = update.get("callback_query")
        if callback_query:
            data = (callback_query.get("data") or "").strip()
            cq_from = callback_query.get("from") or {} # cq means callback query
            message = callback_query.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            callback_query_id = callback_query.get("id")

            if chat_id is None:
                if callback_query_id:
                    bot.answer_callback_query(callback_query_id, text="No chat id", show_alert=False)
                return "", 200

            # manual_offset:... -> create manual reminder
            if data.startswith("manual_offset:"):
                if repo is None:
                    logger.error("Sheets repo not configured; cannot create manual reminder.")
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Storage not configured.",
                            show_alert=True,
                        )
                    return "", 200

                # Check we have pending description for this chat
                state = manual_new_state.get(chat_id)
                if not state or state.get("stage") != "awaiting_offset":
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="No pending reminder.",
                            show_alert=False,
                        )
                    return "", 200

                description = state.get("description") or ""
                offset_key = data.split(":", 1)[1]  # "1h", "1d", "3d", "1w"
                delta = offset_key_to_delta(offset_key)
                if delta is None:
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Unknown offset.",
                            show_alert=False,
                        )
                    return "", 200

                tz = ZoneInfo(settings.timezone)
                now = datetime.now(tz)
                due_at = now + delta

                reminder = Reminder(
                    reminder_id=str(uuid.uuid4()),
                    source_type="manual",
                    gmail_message_id=None,
                    subject=None,
                    sender=None,
                    recipient=None,
                    description=description,
                    telegram_chat_id=settings.telegram_user_id or chat_id,
                    due_at=due_at,
                    status="pending",
                )

                try:
                    repo.create_reminder(reminder)
                except Exception as e:
                    logger.exception("Error creating manual reminder from callback")
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Failed to save reminder.",
                            show_alert=True,
                        )
                    return "", 200

                # Clear state
                manual_new_state.pop(chat_id, None)

                # Stop spinner & confirm
                if callback_query_id:
                    bot.answer_callback_query(
                        callback_query_id,
                        text="Reminder created.",
                        show_alert=False,
                    )

                bot.send_message(
                    chat_id=chat_id,
                    text=f"Reminder created for {due_at.isoformat()}:\n{description}",
                )

                return "", 200

            # Other callback types (for email reminders etc.) will go here later.
            return "", 200

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

    @app.route("/test-create-reminder", methods=["POST"])
    def test_create_reminder():
        """
        Create a dummy manual reminder due in 5 minute.
        This tests:
          - Schema
          - create_reminder()
          - datetime storage
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

        # Use the app timezone (Asia/Singapore by default)
        tz = ZoneInfo(app.config["SETTINGS"].timezone)
        now = datetime.now(tz)
        due_at = now + timedelta(minutes=5)

        reminder = Reminder(
            reminder_id=str(uuid.uuid4()),
            source_type="manual",
            gmail_message_id=None,
            subject=None,
            sender=None,
            recipient=None,
            description="Test reminder from /test-create-reminder",
            telegram_chat_id=settings.telegram_user_id or 0,
            due_at=due_at,
            status="pending",
        )

        try:
            repo.create_reminder(reminder)
        except Exception as e:
            logger.exception("Error creating test reminder")
            return jsonify({"ok": False, "error": str(e)}), 500

        return jsonify(
            {
                "ok": True,
                "reminder_id": reminder.reminder_id,
                "due_at": reminder.due_at.isoformat(),
            }
        )

    @app.route("/test-list-reminders", methods=["GET"])
    def test_list_reminders():
        """
        Return:
          - all reminders
          - reminders that are due as of 'now'
        """
        repo: ReminderSheetRepository | None = app.config.get("REMINDER_REPO")
        if repo is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Sheets repo not configured.",
                    }
                ),
                500,
            )

        tz = ZoneInfo(app.config["SETTINGS"].timezone)
        now = datetime.now(tz)

        try:
            all_reminders = repo.get_all_reminders()
            due_reminders = repo.get_due_reminders(now)
        except Exception as e:
            logger.exception("Error listing reminders")
            return jsonify({"ok": False, "error": str(e)}), 500

        def serialize(r: Reminder) -> dict:
            return {
                "reminder_id": r.reminder_id,
                "source_type": r.source_type,
                "gmail_message_id": r.gmail_message_id,
                "subject": r.subject,
                "sender": r.sender,
                "recipient": r.recipient,
                "description": r.description,
                "telegram_chat_id": r.telegram_chat_id,
                "due_at": r.due_at.isoformat(),
                "status": r.status,
                "row_number": r.row_number,
            }

        return jsonify(
            {
                "ok": True,
                "now": now.isoformat(),
                "all_reminders": [serialize(r) for r in all_reminders],
                "due_reminders": [serialize(r) for r in due_reminders],
            }
        )

    @app.route("/test-gmail-labels", methods=["GET"])
    def test_gmail_labels():
        """
        Dev-only endpoint to verify Gmail OAuth is working.

        Returns the list of labels for the authorised Gmail account.
        """
        gmail_client: GmailClient | None = app.config.get("GMAIL_CLIENT")
        if gmail_client is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Gmail client not configured. Check GMAIL_OAUTH_TOKEN_JSON.",
                    }
                ),
                500,
            )

        try:
            labels = gmail_client.list_labels()
        except Exception as e:
            logger.exception("Error during /test-gmail-labels")
            return jsonify({"ok": False, "error": str(e)}), 500

        return jsonify(
            {
                "ok": True,
                "count": len(labels),
                "labels": [
                    {"id": lbl.get("id"), "name": lbl.get("name")} for lbl in labels
                ],
            }
        ), 200

    @app.route("/test-gmail-recent", methods=["GET"])
    def test_gmail_recent():
        """
        Dev-only endpoint to fetch a few recent emails and show minimal metadata.
        This proves we can read messages without touching bodies/attachments.
        """
        from gmail_client import GmailClient  # avoid circular type issues

        gmail_client: GmailClient | None = app.config.get("GMAIL_CLIENT")
        if gmail_client is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Gmail client not configured. Check GMAIL_OAUTH_TOKEN_JSON.",
                    }
                ),
                500,
            )

        try:
            # Get up to 5 most recent messages from the INBOX
            message_ids = gmail_client.list_recent_message_ids(
                max_results=5,
                label_ids=["INBOX"],
            )
            metadata_list = [
                gmail_client.get_message_metadata(mid) for mid in message_ids
            ]
        except Exception as e:
            logger.exception("Error during /test-gmail-recent")
            return jsonify({"ok": False, "error": str(e)}), 500

        return jsonify(
            {
                "ok": True,
                "count": len(metadata_list),
                "messages": metadata_list,
            }
        ), 200

    return app


module_level_app = create_app()

if __name__ == "__main__":
    # For local development only. On Render, we'll use gunicorn.
    module_level_app.run(host="0.0.0.0", port=5001, debug=True)