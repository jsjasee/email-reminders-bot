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
            cq_from = callback_query.get("from") or {}  # cq means callback query
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

            # ------- email_action:... -> Set reminder / Done for email ------- #
            if data.startswith("email_action:"):
                # format: email_action:<action>:<gmail_message_id>
                parts = data.split(":", 2)
                if len(parts) != 3:
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Invalid email action.",
                            show_alert=False,
                        )
                    return "", 200

                _, action, gmail_message_id = parts

                if action == "set":
                    # Show offset buttons for this email
                    keyboard = bot.build_email_offset_keyboard(gmail_message_id)
                    bot.send_message(
                        chat_id=chat_id,
                        text="When should I remind you about this email?",
                        reply_markup=keyboard,
                    )
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Choose when to be reminded.",
                            show_alert=False,
                        )
                    return "", 200

                if action == "done":
                    # No reminder created, just acknowledge
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Marked as done. No reminder created.",
                            show_alert=False,
                        )
                    # Optional confirmation message
                    bot.send_message(
                        chat_id=chat_id,
                        text="Okay, I won't create a reminder for this email.",
                    )
                    return "", 200

                # Unknown email action
                if callback_query_id:
                    bot.answer_callback_query(
                        callback_query_id,
                        text="Unknown email action.",
                        show_alert=False,
                    )
                return "", 200

            # ------- email_offset:... -> create email-based reminder ------- #
            if data.startswith("email_offset:"):
                # format: email_offset:<gmail_message_id>:<key>
                parts = data.split(":", 2)
                if len(parts) != 3:
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Invalid email offset.",
                            show_alert=False,
                        )
                    return "", 200

                _, gmail_message_id, offset_key = parts

                if repo is None:
                    logger.error("Sheets repo not configured; cannot create email reminder.")
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Storage not configured.",
                            show_alert=True,
                        )
                    return "", 200

                # Need Gmail client to fetch metadata
                gmail_client = app.config.get("GMAIL_CLIENT")
                if gmail_client is None:
                    logger.error("Gmail client not configured; cannot create email reminder.")
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Email access not configured.",
                            show_alert=True,
                        )
                    return "", 200

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

                try:
                    meta = gmail_client.get_message_metadata(gmail_message_id)
                except Exception:
                    logger.exception("Error fetching Gmail metadata for email reminder.")
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Failed to read email metadata.",
                            show_alert=True,
                        )
                    return "", 200

                subject = meta.get("subject")
                sender = meta.get("from")
                recipient = meta.get("to")

                reminder = Reminder(
                    reminder_id=str(uuid.uuid4()),
                    source_type="email",
                    gmail_message_id=gmail_message_id,
                    subject=subject,
                    sender=sender,
                    recipient=recipient,
                    description=None,
                    telegram_chat_id=settings.telegram_user_id or chat_id,
                    due_at=due_at,
                    status="pending",
                )

                try:
                    repo.create_reminder(reminder)
                except Exception:
                    logger.exception("Error creating email reminder from callback")
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Failed to save reminder.",
                            show_alert=True,
                        )
                    return "", 200

                if callback_query_id:
                    bot.answer_callback_query(
                        callback_query_id,
                        text="Email reminder created.",
                        show_alert=False,
                    )

                summary_subject = subject or "(no subject)"
                bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"Reminder created for {due_at.isoformat()} "
                        f"for email:\n{summary_subject}"
                    ),
                )

                return "", 200

            # ------- reminder_extend:... -> snooze an existing reminder ------- #
            if data.startswith("reminder_extend:"):
                # format: reminder_extend:<reminder_id>:<key>
                parts = data.split(":", 2)
                if len(parts) != 3:
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Invalid reminder extend action.",
                            show_alert=False,
                        )
                    return "", 200

                _, reminder_id, offset_key = parts

                if repo is None:
                    logger.error("Sheets repo not configured; cannot update reminder.")
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Storage not configured.",
                            show_alert=True,
                        )
                    return "", 200

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
                new_due_at = now + delta

                try:
                    updated = repo.update_reminder_due_at(reminder_id, new_due_at)
                except Exception:
                    logger.exception("Error updating reminder due_at")
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Failed to snooze reminder.",
                            show_alert=True,
                        )
                    return "", 200

                if not updated:
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Reminder not found.",
                            show_alert=False,
                        )
                    return "", 200

                if callback_query_id:
                    bot.answer_callback_query(
                        callback_query_id,
                        text="Reminder snoozed.",
                        show_alert=False,
                    )

                bot.send_message(
                    chat_id=chat_id,
                    text=f"Reminder snoozed to {new_due_at.isoformat()}.",
                )

                return "", 200

            # ------- reminder_complete:... -> delete reminder ------- #
            if data.startswith("reminder_complete:"):
                # format: reminder_complete:<reminder_id>
                parts = data.split(":", 1)
                if len(parts) != 2:
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Invalid complete action.",
                            show_alert=False,
                        )
                    return "", 200

                _, reminder_id = parts

                if repo is None:
                    logger.error("Sheets repo not configured; cannot delete reminder.")
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Storage not configured.",
                            show_alert=True,
                        )
                    return "", 200

                try:
                    deleted = repo.delete_reminder(reminder_id)
                except Exception:
                    logger.exception("Error deleting reminder")
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Failed to delete reminder.",
                            show_alert=True,
                        )
                    return "", 200

                if callback_query_id:
                    bot.answer_callback_query(
                        callback_query_id,
                        text="Reminder completed." if deleted else "Reminder not found.",
                        show_alert=False,
                    )

                if deleted:
                    bot.send_message(
                        chat_id=chat_id,
                        text="Reminder marked as complete and removed.",
                    )

                return "", 200

            # Other callback types (for future flows) fall through
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
        due_at = now - timedelta(minutes=1) # set the timing to 1 minute in the past so i can instantly get the notif

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

    @app.route("/test-email-notification", methods=["GET", "POST"])
    def test_email_notification():
        """
        Dev-only endpoint:

        - Takes the most recent INBOX email
        - Fetches minimal metadata (From, Subject)
        - Sends a Telegram message to the configured user with:
            New email
            From: ...
            Subject: ...
          plus Set reminder / Done buttons.

        This simulates what the real /gmail-webhook will do later.
        """
        bot: TelegramBot | None = app.config.get("TELEGRAM_BOT")
        gmail_client = app.config.get("GMAIL_CLIENT")
        settings: Settings = app.config["SETTINGS"]

        if bot is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Telegram bot not configured. Check TELEGRAM_BOT_TOKEN / TELEGRAM_USER_ID.",
                    }
                ),
                500,
            )

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

        if settings.telegram_user_id is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "TELEGRAM_USER_ID not configured.",
                    }
                ),
                500,
            )

        try:
            # Get the most recent message in INBOX
            message_ids = gmail_client.list_recent_message_ids(
                max_results=1,
                label_ids=["INBOX"],
            )
            if not message_ids:
                return jsonify({"ok": False, "error": "No messages found in INBOX."}), 200

            gmail_message_id = message_ids[0]
            meta = gmail_client.get_message_metadata(gmail_message_id)
        except Exception as e:
            logger.exception("Error fetching recent Gmail message for test notification")
            return jsonify({"ok": False, "error": str(e)}), 500

        from_header = meta.get("from") or "(unknown sender)"
        subject = meta.get("subject") or "(no subject)"

        text = f"New email\nFrom: {from_header}\nSubject: {subject}"

        keyboard = bot.build_email_action_keyboard(gmail_message_id)

        try:
            bot.send_message(
                chat_id=settings.telegram_user_id,
                text=text,
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.exception("Error sending test email notification to Telegram")
            return jsonify({"ok": False, "error": str(e)}), 500

        return jsonify(
            {
                "ok": True,
                "gmail_message_id": gmail_message_id,
            }
        ), 200

    @app.route("/dispatch-due-reminders", methods=["GET", "POST"])
    def dispatch_due_reminders():
        """
        Called by external cron (or manually) every minute.

        Behaviour:
          - Find reminders with status = "pending" and due_at <= now.
          - For each, send a Telegram message with control buttons:
                +1h / +1d / +3d / +1w / Complete

        NOTE: As implemented, if you don't act on a reminder,
        it will be sent again on each call (every minute) until you
        snooze or complete it.
        """
        bot: TelegramBot | None = app.config.get("TELEGRAM_BOT")
        repo: ReminderSheetRepository | None = app.config.get("REMINDER_REPO")
        settings: Settings = app.config["SETTINGS"]

        if bot is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Telegram bot not configured.",
                    }
                ),
                500,
            )

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

        tz = ZoneInfo(settings.timezone)
        now = datetime.now(tz)

        try:
            due_reminders = repo.get_due_reminders(now)
        except Exception as e:
            logger.exception("Error fetching due reminders")
            return jsonify({"ok": False, "error": str(e)}), 500

        dispatched = 0

        for r in due_reminders:
            # Decide what text to show
            if r.source_type == "email":
                subject = r.subject or "(no subject)"
                sender = r.sender or "(unknown sender)"
                text = f"Reminder (email):\nFrom: {sender}\nSubject: {subject}"
            else:
                desc = r.description or "(no description)"
                text = f"Reminder:\n{desc}"

            keyboard = bot.build_reminder_control_keyboard(r.reminder_id)

            chat_id = r.telegram_chat_id or (settings.telegram_user_id or 0)

            try:
                bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=keyboard,
                )
                dispatched += 1

                # Mark as notified so we don't send it again
                try:
                    updated = repo.update_reminder_status(r.reminder_id, "notified")
                    if not updated:
                        logger.warning(
                            "Failed to update status to 'notified' for reminder_id=%s",
                            r.reminder_id,
                        )
                except Exception:
                    logger.exception(
                        "Error updating reminder status to 'notified' for %s",
                        r.reminder_id,
                    )

            except Exception:
                logger.exception(
                    "Error sending reminder %s to chat_id %s",
                    r.reminder_id,
                    chat_id,
                )
                # continue to next reminder

        return jsonify(
            {
                "ok": True,
                "now": now.isoformat(),
                "dispatched": dispatched,
            }
        ), 200

    return app


module_level_app = create_app()

if __name__ == "__main__":
    # For local development only. On Render, we'll use gunicorn.
    module_level_app.run(host="0.0.0.0", port=5001, debug=True)