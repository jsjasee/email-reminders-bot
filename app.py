import logging
import uuid
import base64  # for decoding Pub/Sub data field
import json

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

# In-memory per-chat state for custom datetime input
# Example structure:
# {
#   chat_id: {
#       "mode": "manual",
#       "description": str,
#       "original_chat_id": int,
#       "original_message_id": int | None,
#   }
# }
custom_datetime_state: Dict[int, Dict[str, Any]] = {}

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

    # Try to load persisted Gmail historyId from Sheets Config via ReminderSheetRepository
    top_level_last_history_id: Optional[str] = None # the top_level parts refers to how the json is structured - this is historyId is the historyId of the batch of records in the json
    if gmail_client is not None and sheets_repo is not None:
        top_level_last_history_id = sheets_repo.read_config_value("last_history_id")
        if top_level_last_history_id:
            logger.info("Loaded last_history_id=%s from Sheets Config", top_level_last_history_id)
        else:
            logger.info("No last_history_id found in Sheets Config; starting fresh.")
    elif gmail_client is not None:
        logger.info("Sheets repo missing; LAST_HISTORY_ID will remain in-memory only.")

    app.config["LAST_HISTORY_ID"] = top_level_last_history_id

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

        Behaviour:
        - Enforces single-user rule.
        - /start: simple welcome message.
        - /new: ask for reminder description.
        - Next plain-text message after /new: treat as description, show offset buttons.
        - Button press (manual_offset:...): create manual reminder in Sheets and edit message.
        - Email notification actions and reminder control buttons also edit the original message
          and remove keyboards so buttons cannot be spammed indefinitely.
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

        # ------- Helper: parse custom datetime DD/MM/YYYY HH:MM -> aware datetime ------- #
        def parse_custom_datetime(date_text: str, date_tz: ZoneInfo) -> Optional[datetime]:
            """
            Expected format: DD/MM/YYYY HH:MM (24h clock), e.g. 25/12/2025 14:30.
            Returns a timezone-aware datetime in the given date_tz, or None on parse error.
            """
            try:
                dt_naive = datetime.strptime(date_text, "%d/%m/%Y %H:%M")
            except ValueError:
                return None
            return dt_naive.replace(tzinfo=date_tz)

        # ------- Handle normal messages (commands / description / custom datetime) ------- #
        message = update.get("message")
        if message:
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            text = (message.get("text") or "").strip()

            if chat_id is None:
                return "", 200

            # 0) If this chat is waiting for a custom datetime, handle that first
            state_custom = custom_datetime_state.get(chat_id)
            if state_custom:
                tz = ZoneInfo(settings.timezone)

                parsed_dt = parse_custom_datetime(text, tz)
                if parsed_dt is None:
                    bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "Invalid format. Please use DD/MM/YYYY HH:MM "
                            "(e.g. 25/12/2025 14:30)."
                        ),
                    )
                    return "", 200

                mode = state_custom.get("mode")
                if mode == "manual":
                    # Create manual reminder with this custom datetime
                    repo: ReminderSheetRepository | None = app.config.get("REMINDER_REPO")
                    if repo is None:
                        logger.error("Sheets repo not configured; cannot save custom reminder.")
                        bot.send_message(
                            chat_id=chat_id,
                            text="Storage not configured; could not save reminder.",
                        )
                        custom_datetime_state.pop(chat_id, None)
                        return "", 200

                    description = state_custom.get("description") or ""
                    original_chat_id = state_custom.get("original_chat_id", chat_id)
                    original_message_id = state_custom.get("original_message_id")
                    prompt_message_id = state_custom.get("prompt_message_id")
                    prompt_chat_id = state_custom.get("prompt_chat_id", chat_id)

                    reminder = Reminder(
                        reminder_id=str(uuid.uuid4()),
                        source_type="manual",
                        gmail_message_id=None,
                        subject=None,
                        sender=None,
                        recipient=None,
                        description=description,
                        telegram_chat_id=settings.telegram_user_id or original_chat_id,
                        due_at=parsed_dt,
                        status="pending",
                    )

                    try:
                        repo.create_reminder(reminder)
                    except Exception:
                        logger.exception("Error creating manual reminder from custom datetime input")
                        bot.send_message(
                            chat_id=chat_id,
                            text="Failed to save reminder.",
                        )
                        custom_datetime_state.pop(chat_id, None)
                        return "", 200

                    confirmation_text = (
                        f"✅ Reminder created for {parsed_dt.isoformat()}:\n{description}"
                    )

                    # Prefer editing the original keyboard message; fall back to a new message
                    if original_message_id is not None:
                        try:
                            bot.edit_message_text(
                                chat_id=original_chat_id,
                                message_id=original_message_id,
                                text=confirmation_text,
                                reply_markup=None,  # remove keyboard
                            )
                        except Exception:
                            logger.exception(
                                "Error editing original message for custom manual reminder; "
                                "sending new message instead."
                            )
                            bot.send_message(chat_id=original_chat_id, text=confirmation_text)
                    else:
                        bot.send_message(chat_id=original_chat_id, text=confirmation_text)

                    # NEW: clean up the prompt message (remove Cancel keyboard / close it)
                    if prompt_message_id is not None:
                        try:
                            bot.edit_message_text(
                                chat_id=prompt_chat_id,
                                message_id=prompt_message_id,
                                text="✅ Custom date received.",
                                reply_markup=None,
                            )
                        except Exception:
                            logger.exception(
                                "Error editing custom datetime prompt message after success."
                            )

                    # Clear custom state for this chat
                    custom_datetime_state.pop(chat_id, None)
                    return "", 200

                elif mode == "email":
                    # Email custom datetime: create email-based reminder at parsed_dt
                    repo: ReminderSheetRepository | None = app.config.get("REMINDER_REPO")
                    gmail_client = app.config.get("GMAIL_CLIENT")
                    if repo is None or gmail_client is None:
                        logger.error(
                            "Missing repo or gmail_client; cannot save email custom reminder."
                        )
                        bot.send_message(
                            chat_id=chat_id,
                            text="Storage or email access not configured; could not save reminder.",
                        )
                        custom_datetime_state.pop(chat_id, None)
                        return "", 200

                    gmail_message_id = state_custom.get("gmail_message_id")
                    original_chat_id = state_custom.get("original_chat_id", chat_id)
                    original_message_id = state_custom.get("original_message_id")
                    original_text = state_custom.get("original_text") or ""
                    prompt_message_id = state_custom.get("prompt_message_id")
                    prompt_chat_id = state_custom.get("prompt_chat_id", chat_id)

                    # Fetch email metadata
                    try:
                        meta = gmail_client.get_message_metadata(gmail_message_id)
                    except Exception:
                        logger.exception("Error fetching Gmail metadata for email custom reminder.")
                        bot.send_message(
                            chat_id=chat_id,
                            text="Failed to read email metadata; reminder not created.",
                        )
                        custom_datetime_state.pop(chat_id, None)
                        return "", 200

                    subject = meta.get("subject")
                    sender = meta.get("from")
                    recipient = meta.get("original_recipient") or meta.get("to") # only default to "to" aka the target email address if the recipient email address we wanna get is empty

                    reminder = Reminder(
                        reminder_id=str(uuid.uuid4()),
                        source_type="email",
                        gmail_message_id=gmail_message_id,
                        subject=subject,
                        sender=sender,
                        recipient=recipient,
                        description=None,
                        telegram_chat_id=settings.telegram_user_id or original_chat_id,
                        due_at=parsed_dt,
                        status="pending",
                    )

                    try:
                        repo.create_reminder(reminder)
                    except Exception:
                        logger.exception("Error creating email reminder from custom datetime input")
                        bot.send_message(
                            chat_id=chat_id,
                            text="Failed to save email reminder.",
                        )
                        custom_datetime_state.pop(chat_id, None)
                        return "", 200

                    # Edit original email card
                    summary_subject = subject or "(no subject)"
                    if original_text:
                        new_text = (
                                original_text
                                + f"\n\n✅ Reminder created for {parsed_dt.isoformat()}."
                        )
                    else:
                        new_text = (
                            f"Reminder created for {parsed_dt.isoformat()} "
                            f"for email:\n{summary_subject}"
                        )

                    if original_message_id is not None:
                        try:
                            bot.edit_message_text(
                                chat_id=original_chat_id,
                                message_id=original_message_id,
                                text=new_text,
                                reply_markup=None,  # remove keyboard
                            )
                        except Exception:
                            logger.exception(
                                "Error editing original email message for custom reminder; "
                                "sending new message instead."
                            )
                            bot.send_message(chat_id=original_chat_id, text=new_text)
                    else:
                        bot.send_message(chat_id=original_chat_id, text=new_text)

                    # Clean up custom prompt message
                    if prompt_message_id is not None:
                        try:
                            bot.edit_message_text(
                                chat_id=prompt_chat_id,
                                message_id=prompt_message_id,
                                text="✅ Custom date received for this email.",
                                reply_markup=None,
                            )
                        except Exception:
                            logger.exception(
                                "Error editing email custom datetime prompt message after success."
                            )

                    custom_datetime_state.pop(chat_id, None)
                    return "", 200

                elif mode == "snooze":
                    # Custom snooze: update due_at to parsed_dt
                    repo: ReminderSheetRepository | None = app.config.get("REMINDER_REPO")
                    if repo is None:
                        logger.error("Sheets repo not configured; cannot snooze reminder.")
                        bot.send_message(
                            chat_id=chat_id,
                            text="Storage not configured; could not snooze reminder.",
                        )
                        custom_datetime_state.pop(chat_id, None)
                        return "", 200

                    reminder_id = state_custom.get("reminder_id")
                    original_chat_id = state_custom.get("original_chat_id", chat_id)
                    original_message_id = state_custom.get("original_message_id")
                    original_text = state_custom.get("original_text") or "Reminder"
                    prompt_message_id = state_custom.get("prompt_message_id")
                    prompt_chat_id = state_custom.get("prompt_chat_id", chat_id)

                    try:
                        updated = repo.update_reminder_due_at(reminder_id, parsed_dt)
                    except Exception:
                        logger.exception("Error updating reminder due_at (custom snooze)")
                        bot.send_message(
                            chat_id=chat_id,
                            text="Failed to snooze reminder.",
                        )
                        custom_datetime_state.pop(chat_id, None)
                        return "", 200

                    if not updated:
                        bot.send_message(
                            chat_id=chat_id,
                            text="Reminder not found.",
                        )
                        custom_datetime_state.pop(chat_id, None)
                        return "", 200

                    new_text = original_text + f"\n\n⏰ Snoozed to {parsed_dt.isoformat()}."

                    # Edit the original reminder card
                    if original_message_id is not None:
                        try:
                            bot.edit_message_text(
                                chat_id=original_chat_id,
                                message_id=original_message_id,
                                text=new_text,
                                reply_markup=None,  # remove keyboard
                            )
                        except Exception:
                            logger.exception(
                                "Error editing reminder message after custom snooze; "
                                "sending new message instead."
                            )
                            bot.send_message(chat_id=original_chat_id, text=new_text)
                    else:
                        bot.send_message(chat_id=original_chat_id, text=new_text)

                    # Clean up the custom prompt -> basically means to EDIT that prompt message and remove the keyboard.
                    if prompt_message_id is not None:
                        try:
                            bot.edit_message_text(
                                chat_id=prompt_chat_id, # this is the same as our chat_id, so technically don't need this variable
                                message_id=prompt_message_id, # we need the message AND chat_id in order to edit the message
                                text="✅ Custom date received. Reminder snoozed.",
                                reply_markup=None,
                            )
                        except Exception:
                            logger.exception(
                                "Error editing custom snooze prompt message after success."
                            )

                    custom_datetime_state.pop(chat_id, None)
                    return "", 200

                    # Unknown mode – just clear and ignore
                custom_datetime_state.pop(chat_id, None)
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

                # Show buttons for +1h/+1d/+3d/+1w (+ Custom will be added in the keyboard)
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
            message_id = message.get("message_id")
            callback_query_id = callback_query.get("id")

            if chat_id is None:
                if callback_query_id:
                    bot.answer_callback_query(
                        callback_query_id,
                        text="No chat id",
                        show_alert=False,
                    )
                return "", 200

            # ------- custom_cancel:mode -> cancel custom datetime flow ------- #
            if data.startswith("custom_cancel:"):
                # Pop any custom datetime state for this chat
                state_custom = custom_datetime_state.pop(chat_id, None)

                # If we were in manual custom mode, restore the awaiting_offset state, so that users can press the buttons "+1h, +1 day, +1 week, custom" again
                if state_custom and state_custom.get("mode") == "manual":
                    description = state_custom.get("description") or ""
                    manual_new_state[chat_id] = {
                        "stage": "awaiting_offset",
                        "description": description,
                    }

                if callback_query_id:
                    bot.answer_callback_query(
                        callback_query_id,
                        text="Custom date entry cancelled.",
                        show_alert=False,
                    )

                # Edit the prompt message (the one with Cancel) to show cancelled and remove keyboard
                if message_id is not None:
                    try:
                        bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text="Custom date entry cancelled.",
                            reply_markup=None,  # remove Cancel button
                        )
                    except Exception:
                        logger.exception("Failed to edit message after custom_cancel callback.")

                return "", 200

            # ------- manual_offset:... -> create manual reminder or enter custom flow ------- #
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
                offset_key = data.split(":", 1)[1]  # "1h", "1d", "3d", "1w" or "custom"

                # NEW: handle custom offset by entering custom datetime state
                if offset_key == "custom":
                    # Seed state with info about the original card
                    custom_datetime_state[chat_id] = {
                        "mode": "manual",
                        "description": description,
                        "original_chat_id": chat_id,
                        "original_message_id": message_id,
                        # prompt_message_id will be filled after send_message
                    }

                    # Once we enter custom mode, this /new flow is no longer in awaiting_offset
                    manual_new_state.pop(chat_id, None)

                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Send a custom date/time.",
                            show_alert=False,
                        )

                    cancel_keyboard = bot.build_custom_datetime_cancel_keyboard("manual")

                    # Send prompt asking for custom datetime, with Cancel button
                    sent = bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "Please type the date and time in DD/MM/YYYY HH:MM "
                            "(e.g. 25/12/2025 14:30)."
                        ),
                        reply_markup=cancel_keyboard,
                    )

                    # Try to record the prompt message_id so we can clean it up later
                    try:
                        if isinstance(sent, dict):
                            prompt_message_id = sent.get("message_id")
                        else:
                            prompt_message_id = getattr(sent, "message_id", None)

                        if prompt_message_id is not None:
                            state_custom = custom_datetime_state.get(chat_id) or {}
                            state_custom["prompt_message_id"] = prompt_message_id
                            state_custom["prompt_chat_id"] = chat_id
                            custom_datetime_state[chat_id] = state_custom
                    except Exception:
                        logger.exception(
                            "Failed to capture prompt_message_id for custom datetime."
                        )

                    return "", 200

                # Existing preset offsets (+1h, +1d, +3d, +1w)
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
                except Exception:
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

                # Stop spinner
                if callback_query_id:
                    bot.answer_callback_query(
                        callback_query_id,
                        text="Reminder created.",
                        show_alert=False,
                    )

                confirmation_text = (
                    f"✅ Reminder created for {due_at.isoformat()}:\n{description}"
                )

                # Prefer editing the original keyboard message; fall back to new message
                if message_id is not None:
                    bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=confirmation_text,
                        reply_markup=None,  # remove keyboard
                    )
                else:
                    bot.send_message(
                        chat_id=chat_id,
                        text=confirmation_text,
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
                    # Replace the Set/Done keyboard with the offset keyboard on the same message
                    keyboard = bot.build_email_offset_keyboard(gmail_message_id)

                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Choose when to be reminded.",
                            show_alert=False,
                        )

                    base_text = message.get("text") or "New email"
                    new_text = base_text + "\n\nWhen should I remind you about this email?"

                    if message_id is not None:
                        bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=new_text,
                            reply_markup=keyboard,  # new keyboard
                        )
                    else:
                        bot.send_message(
                            chat_id=chat_id,
                            text="When should I remind you about this email?",
                            reply_markup=keyboard,
                        )
                    return "", 200

                if action == "done":
                    # No reminder created, just acknowledge + update card
                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Marked as done. No reminder created.",
                            show_alert=False,
                        )

                    base_text = message.get("text") or "New email"
                    new_text = base_text + "\n\n✅ Marked as done. No reminder created."

                    if message_id is not None:
                        bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=new_text,
                            reply_markup=None,  # remove keyboard
                        )
                    else:
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

            # ------- email_offset:... -> create email-based reminder or enter custom flow ------- #
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

                # Need Gmail client for email-based reminders
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

                # --- NEW: email custom datetime path --- #
                if offset_key == "custom":
                    # Record enough state to finish later when user sends the datetime
                    original_text = message.get("text") or ""

                    custom_datetime_state[chat_id] = {
                        "mode": "email",
                        "gmail_message_id": gmail_message_id,
                        "original_chat_id": chat_id,
                        "original_message_id": message_id,
                        "original_text": original_text,
                        # prompt_message_id will be set after sending prompt
                    }

                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Send a custom date/time.",
                            show_alert=False,
                        )

                    cancel_keyboard = bot.build_custom_datetime_cancel_keyboard("email")

                    sent = bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "Please type the date and time in DD/MM/YYYY HH:MM "
                            "(e.g. 25/12/2025 14:30)."
                        ),
                        reply_markup=cancel_keyboard,
                    )

                    # Capture prompt_message_id so we can clean it up on success/cancel
                    try:
                        if isinstance(sent, dict):
                            prompt_message_id = sent.get("message_id")
                        else:
                            prompt_message_id = getattr(sent, "message_id", None)

                        if prompt_message_id is not None:
                            state_custom = custom_datetime_state.get(chat_id) or {}
                            state_custom["prompt_message_id"] = prompt_message_id
                            state_custom["prompt_chat_id"] = chat_id
                            custom_datetime_state[chat_id] = state_custom
                    except Exception:
                        logger.exception(
                            "Failed to capture prompt_message_id for email custom datetime."
                        )

                    return "", 200

                # --- Existing preset offsets (+1h, +1d, +3d, +1w) --- #
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
                recipient = meta.get("original_recipient") or meta.get("to")

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

                base_text = message.get("text") or ""
                summary_subject = subject or "(no subject)"

                if base_text:
                    new_text = base_text + f"\n\n✅ Reminder created for {due_at.isoformat()}."
                else:
                    new_text = (
                        f"Reminder created for {due_at.isoformat()} "
                        f"for email:\n{summary_subject}"
                    )

                if message_id is not None:
                    bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=new_text,
                        reply_markup=None,  # remove keyboard
                    )
                else:
                    bot.send_message(
                        chat_id=chat_id,
                        text=new_text,
                    )

                return "", 200

            # ------- reminder_extend:... -> snooze an existing reminder or enter custom flow ------- #
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

                # --- NEW: custom snooze path --- #
                if offset_key == "custom":
                    base_text = message.get("text") or "Reminder"

                    # Seed state so the next text message is interpreted as custom datetime
                    custom_datetime_state[chat_id] = {
                        "mode": "snooze",
                        "reminder_id": reminder_id,
                        "original_chat_id": chat_id,
                        "original_message_id": message_id,
                        "original_text": base_text,
                        # prompt_message_id will be filled after sending prompt
                    }

                    if callback_query_id:
                        bot.answer_callback_query(
                            callback_query_id,
                            text="Send a custom date/time.",
                            show_alert=False,
                        )

                    cancel_keyboard = bot.build_custom_datetime_cancel_keyboard("snooze")

                    sent = bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "Send a new date/time for this reminder in "
                            "DD/MM/YYYY HH:MM (e.g. 25/12/2025 14:30)."
                        ),
                        reply_markup=cancel_keyboard,
                    )

                    # Capture prompt_message_id so we can clean it up later
                    try:
                        if isinstance(sent, dict):
                            prompt_message_id = sent.get("message_id")
                        else:
                            prompt_message_id = getattr(sent, "message_id", None)

                        if prompt_message_id is not None:
                            state_custom = custom_datetime_state.get(chat_id) or {}
                            state_custom["prompt_message_id"] = prompt_message_id
                            state_custom["prompt_chat_id"] = chat_id
                            custom_datetime_state[chat_id] = state_custom
                    except Exception:
                        logger.exception(
                            "Failed to capture prompt_message_id for snooze custom datetime."
                        )

                    return "", 200

                # --- Existing fixed-offset snooze (+1h, +1d, +3d, +1w) --- #
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

                base_text = message.get("text") or "Reminder"
                new_text = base_text + f"\n\n⏰ Snoozed to {new_due_at.isoformat()}."

                if message_id is not None:
                    bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=new_text,
                        reply_markup=None,  # remove keyboard after one snooze
                    )
                else:
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

                if not deleted:
                    return "", 200

                base_text = message.get("text") or "Reminder"
                new_text = base_text + "\n\n✅ Reminder marked as complete and removed."

                if message_id is not None:
                    bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=new_text,
                        reply_markup=None,  # remove keyboard
                    )
                else:
                    bot.send_message(
                        chat_id=chat_id,
                        text="Reminder marked as complete and removed.",
                    )

                return "", 200

            # Other callback types (for future flows) fall through
            return "", 200

        # For now, ignore other update types.
        return "", 200

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

        if app.config.get("GMAIL_CLIENT") is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Gmail client not configured. Check GMAIL_OAUTH_TOKEN_JSON.",
                    }
                ),
                500,
            )

        if app.config["SETTINGS"].telegram_user_id is None:
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

        original_recipient = meta.get("original_recipient")

        if original_recipient:
            text = (
                "New email\n"
                f"From: {from_header}\n"
                f"To: {original_recipient}\n"
                f"Subject: {subject}"
            )
        else:
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

    @app.route("/debug/setup-gmail-watch", methods=["POST"])
    def debug_setup_gmail_watch():
        resp = app.config["GMAIL_CLIENT"].setup_watch()
        return jsonify(
            {
                "ok": True,
                "watch_response": resp,
            }
        ), 200

    @app.route("/test-gmail-history", methods=["GET"])
    def test_gmail_history():
        """
        Dev-only endpoint to inspect Gmail history.

        Usage example:
          curl "http://localhost:5001/test-gmail-history?start_history_id=1234567890"

        Returns:
          - start_history_id (what you passed in)
          - new_message_ids (list of Gmail message IDs)
          - latest_history_id (what you should persist next time)
        """
        if app.config.get("GMAIL_CLIENT") is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Gmail client not configured. Check GMAIL_OAUTH_TOKEN_JSON.",
                    }
                ),
                500,
            )

        start_history_id = request.args.get("start_history_id")
        if not start_history_id:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Missing start_history_id query param.",
                    }
                ),
                400,
            )

        try:
            message_ids, latest_history_id = gmail_client.list_new_message_ids_since(
                start_history_id=start_history_id,
            )
        except Exception as e:
            logger.exception("Error during /test-gmail-history")
            return jsonify({"ok": False, "error": str(e)}), 500

        return jsonify(
            {
                "ok": True,
                "start_history_id": start_history_id,
                "new_message_ids": message_ids,
                "latest_history_id": latest_history_id,
            }
        ), 200

    @app.route("/gmail-webhook", methods=["POST"])
    def gmail_webhook():
        """
        Gmail Pub/Sub push endpoint.

        Step 1 behaviour (this step):
          - Decode Pub/Sub envelope
          - Extract emailAddress + historyId
          - Use LAST_HISTORY_ID from app.config:
              * If None -> bootstrap: set LAST_HISTORY_ID = historyId, do nothing else.
              * Else    -> call Gmail history API for changes since LAST_HISTORY_ID,
                           log new message IDs, then update LAST_HISTORY_ID.
          - Always return 200 so Pub/Sub doesn’t retry.

        NOTE: No Telegram sends, no sender filter yet.
        """
        gmail_client: GmailClient | None = app.config.get("GMAIL_CLIENT")
        bot: TelegramBot | None = app.config.get("TELEGRAM_BOT")
        settings: Settings = app.config["SETTINGS"]
        repo: ReminderSheetRepository | None = app.config.get("REMINDER_REPO")

        if gmail_client is None:
            logger.error("Gmail client not configured; cannot process Gmail webhook.")
            return jsonify({"ok": False, "error": "gmail_client_not_configured"}), 200

        envelope = request.get_json(silent=True) or {}
        logger.info("Received Pub/Sub envelope: %s", envelope)

        message = envelope.get("message") or {}
        data_b64 = message.get("data")

        if not data_b64:
            logger.warning("Pub/Sub message missing 'data'; ignoring.")
            return jsonify({"ok": True, "ignored": True, "reason": "no data"}), 200

        # Decode inner Gmail notification
        try:
            payload_bytes = base64.b64decode(data_b64)
            payload_str = payload_bytes.decode("utf-8")
            gmail_notification = json.loads(payload_str)
        except Exception:
            logger.exception("Failed to decode/parse Gmail Pub/Sub data")
            return jsonify({"ok": False, "error": "decode_failed"}), 200

        email_address = gmail_notification.get("emailAddress")
        history_id = gmail_notification.get("historyId")

        logger.info(
            "Gmail push notification: emailAddress=%s, historyId=%s",
            email_address,
            history_id,
        )

        # In-memory last processed historyId
        last_history_id = app.config.get("LAST_HISTORY_ID")

        # 1) First time: bootstrap LAST_HISTORY_ID, do not call history.list yet
        if last_history_id is None:
            app.config["LAST_HISTORY_ID"] = history_id
            logger.info(
                "Bootstrapping LAST_HISTORY_ID to %s (no history processed this time).",
                history_id,
            )

            # Persist bootstrap value via repo if available
            if repo is not None and history_id is not None:
                try:
                    repo.write_config_value("last_history_id", str(history_id))
                except Exception:
                    logger.exception(
                        "Failed to persist bootstrap last_history_id=%s to Config sheet",
                        history_id,
                    )

            return jsonify(
                {
                    "ok": True,
                    "mode": "bootstrap",
                    "emailAddress": email_address,
                    "historyId": history_id,
                }
            ), 200

        # 2) Subsequent calls: list new messages since last_history_id
        try:
            new_message_ids, latest_history_id = gmail_client.list_new_message_ids_since(
                start_history_id=str(last_history_id),
                # keep default label_ids=["INBOX"] inside the method
            )
        except Exception as e:
            logger.exception("Error while listing Gmail history in /gmail-webhook")
            # Do NOT change LAST_HISTORY_ID on error.
            return jsonify(
                {
                    "ok": False,
                    "error": str(e),
                    "emailAddress": email_address,
                    "historyId": history_id,
                }
            ), 200

        logger.info(
            "History diff: last_history_id=%s, new_message_ids=%s, latest_history_id=%s",
            last_history_id,
            new_message_ids,
            latest_history_id,
        )

        # Update LAST_HISTORY_ID if we got a newer one
        if latest_history_id is not None and latest_history_id != last_history_id:
            app.config["LAST_HISTORY_ID"] = latest_history_id
            logger.info("Updated LAST_HISTORY_ID to %s", latest_history_id)

            # Persist updated value via repo if available
            if repo is not None:
                try:
                    repo.write_config_value("last_history_id", str(latest_history_id))
                except Exception:
                    logger.exception(
                        "Failed to persist updated last_history_id=%s to Config sheet",
                        latest_history_id,
                    )

        # ---- NEW: fetch metadata + filter by sender email ---- #
        matched_messages: list[dict[str, Any]] = []

        for msg_id in new_message_ids:
            try:
                meta = gmail_client.get_message_metadata(msg_id)
            except Exception:
                logger.exception(
                    "Failed to fetch metadata for Gmail message_id=%s in /gmail-webhook",
                    msg_id,
                )
                continue

            from_header = meta.get("from") or ""
            subject = meta.get("subject") or "(no subject)"
            original_recipient = meta.get("original_recipient")

            # Normalise for case-insensitive substring match
            if settings.target_sender_email.lower() in from_header.lower():
                logger.info(
                    "Matched target sender %s for message_id=%s: from=%r, subject=%r",
                    settings.target_sender_email,
                    msg_id,
                    from_header,
                    subject,
                )
                matched_messages.append(
                    {
                        "gmail_message_id": msg_id,
                        "from": from_header,
                        "subject": subject,
                        "original_recipient": original_recipient,
                    }
                )
            else:
                logger.info(
                    "Ignoring message_id=%s: sender %r does not match %s",
                    msg_id,
                    from_header,
                    settings.target_sender_email,
                )

        # ---- NEW: send Telegram cards for matched messages ---- #
        telegram_dispatched = 0

        if bot is None:
            logger.warning(
                "Telegram bot not configured; cannot send notifications for matched messages."
            )
        elif settings.telegram_user_id is None:
            logger.warning(
                "TELEGRAM_USER_ID not configured; cannot send notifications for matched messages."
            )
        else:
            for mm in matched_messages:
                gmail_message_id = mm["gmail_message_id"]
                from_header = mm["from"] or "(unknown sender)"
                subject = mm["subject"] or "(no subject)"
                original_recipient = mm.get("original_recipient")

                if original_recipient:
                    text = (
                        "New email\n"
                        f"From: {from_header}\n"
                        f"To: {original_recipient}\n"
                        f"Subject: {subject}"
                    )
                else:
                    text = f"New email\nFrom: {from_header}\nSubject: {subject}"

                keyboard = bot.build_email_action_keyboard(gmail_message_id)

                try:
                    bot.send_message(
                        chat_id=settings.telegram_user_id,
                        text=text,
                        reply_markup=keyboard,
                    )
                    telegram_dispatched += 1
                    logger.info(
                        "Sent Telegram notification for Gmail message_id=%s",
                        gmail_message_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to send Telegram notification for Gmail message_id=%s",
                        gmail_message_id,
                    )
        # Return a simple json
        return jsonify(
            {
                "ok": True,
                "mode": "history",
                "emailAddress": email_address,
                "push_historyId": history_id,
                "last_history_id_before": last_history_id,
                "new_message_ids": new_message_ids,
                "latest_history_id": latest_history_id,
                "last_history_id_after": app.config.get("LAST_HISTORY_ID"),
                "matched_messages": matched_messages,
                "telegram_dispatched": telegram_dispatched,
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
                recipient = r.recipient
                text = f"Reminder (email):\nFrom: {sender}\nTo: {recipient}\nSubject: {subject}"
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