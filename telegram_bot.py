import logging
from typing import Optional, Dict, Any

import telebot
from telebot import types


logger = logging.getLogger(__name__)


class TelegramBot:
    """
    Thin wrapper around telebot.TeleBot.

    Exposed interface:
      - send_message(chat_id, text, reply_markup=None)
      - is_allowed_user(update_dict)
      - build_manual_offset_keyboard()
      - answer_callback_query(callback_query_id, text=None, show_alert=False)
    """

    def __init__(self, token: str, allowed_user_id: Optional[int]):
        self.allowed_user_id = allowed_user_id
        # We won't use telebot's polling or decorator system; just its client.
        self.bot = telebot.TeleBot(token, parse_mode=None)

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Optional[Any] = None,
    ):
        """
        Send a message via Telegram.

        reply_markup can be:
          - None
          - a telebot.types.InlineKeyboardMarkup, etc.
        """
        try:
            msg = self.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
            )
            return msg  # telebot Message object
        except Exception:
            logger.exception("Error sending Telegram message")
            raise

    def edit_message_text(self,chat_id: int,message_id: int,text: str,reply_markup: Any | None = None,) -> None:
        """
        Edit an existing Telegram message (and optionally replace/remove its keyboard).
        """
        try:
            self.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
        except Exception:
            logger.exception(
                "Error editing Telegram message chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )

    def is_allowed_user(self, update: Dict[str, Any]) -> bool:
        """
        Only allow messages / callbacks from the single configured user.
        If allowed_user_id is None, allow all users (for very early local tests).
        """
        if self.allowed_user_id is None:
            return True

        # Normal message
        if "message" in update:
            from_user = (update["message"].get("from") or {})
            return from_user.get("id") == self.allowed_user_id

        # Callback query
        if "callback_query" in update:
            from_user = (update["callback_query"].get("from") or {})
            return from_user.get("id") == self.allowed_user_id

        # Other update types we don't recognise yet
        return False

    # --------- Helpers for manual reminders --------- #

    def build_manual_offset_keyboard(self) -> types.InlineKeyboardMarkup:
        """
        Inline keyboard with +1h / +1d / +3d / +1w for manual reminders.
        callback_data uses 'manual_offset:<key>' format.
        """
        markup = types.InlineKeyboardMarkup()
        buttons = [
            types.InlineKeyboardButton("+1 hour", callback_data="manual_offset:1h"),
            types.InlineKeyboardButton("+1 day", callback_data="manual_offset:1d"),
            types.InlineKeyboardButton("+3 days", callback_data="manual_offset:3d"),
            types.InlineKeyboardButton("+1 week", callback_data="manual_offset:1w"),
        ]
        markup.row(buttons[0], buttons[1])
        markup.row(buttons[2], buttons[3])
        return markup

    def answer_callback_query(
        self,
        callback_query_id,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> None:
        """
        Wrap answerCallbackQuery so we can stop the 'loading' spinner and show a short message.
        """
        try:
            # note the below is the answer_callback_query at the bot level, not the TelegramBot level, it's a different function
            self.bot.answer_callback_query(
                callback_query_id,
                text=text,
                show_alert=show_alert,
            )
        except Exception:
            logger.exception("Error answering callback query")

    # --------- Helpers for email-based reminders --------- #

    def build_email_action_keyboard(self, gmail_message_id: str) -> types.InlineKeyboardMarkup:
        """
        Inline keyboard for a 'New email' notification with:
          - Set reminder
          - Done

        callback_data:
          - email_action:set:<gmail_message_id>
          - email_action:done:<gmail_message_id>
        """
        markup = types.InlineKeyboardMarkup()
        btn_set = types.InlineKeyboardButton(
            "Set reminder",
            callback_data=f"email_action:set:{gmail_message_id}",
        )
        btn_done = types.InlineKeyboardButton(
            "Done",
            callback_data=f"email_action:done:{gmail_message_id}",
        )
        markup.row(btn_set, btn_done)
        return markup

    def build_email_offset_keyboard(self, gmail_message_id: str) -> types.InlineKeyboardMarkup:
        """
        Inline keyboard with +1h / +1d / +3d / +1w for email reminders.

        callback_data:
          - email_offset:<gmail_message_id>:1h
          - email_offset:<gmail_message_id>:1d
          - email_offset:<gmail_message_id>:3d
          - email_offset:<gmail_message_id>:1w
        """
        markup = types.InlineKeyboardMarkup()
        buttons = [
            types.InlineKeyboardButton(
                "+1 hour", callback_data=f"email_offset:{gmail_message_id}:1h"
            ),
            types.InlineKeyboardButton(
                "+1 day", callback_data=f"email_offset:{gmail_message_id}:1d"
            ),
            types.InlineKeyboardButton(
                "+3 days", callback_data=f"email_offset:{gmail_message_id}:3d"
            ),
            types.InlineKeyboardButton(
                "+1 week", callback_data=f"email_offset:{gmail_message_id}:1w"
            ),
        ]
        markup.row(buttons[0], buttons[1])
        markup.row(buttons[2], buttons[3])
        return markup

    def build_reminder_control_keyboard(self, reminder_id: str) -> types.InlineKeyboardMarkup:
        """
        Inline keyboard for a due reminder with:
          - +1 hour / +1 day / +3 days / +1 week / Complete

        callback_data:
          - reminder_extend:<reminder_id>:1h
          - reminder_extend:<reminder_id>:1d
          - reminder_extend:<reminder_id>:3d
          - reminder_extend:<reminder_id>:1w
          - reminder_complete:<reminder_id>
        """
        markup = types.InlineKeyboardMarkup()
        buttons = [
            types.InlineKeyboardButton(
                "+1 hour", callback_data=f"reminder_extend:{reminder_id}:1h"
            ),
            types.InlineKeyboardButton(
                "+1 day", callback_data=f"reminder_extend:{reminder_id}:1d"
            ),
            types.InlineKeyboardButton(
                "+3 days", callback_data=f"reminder_extend:{reminder_id}:3d"
            ),
            types.InlineKeyboardButton(
                "+1 week", callback_data=f"reminder_extend:{reminder_id}:1w"
            ),
            types.InlineKeyboardButton(
                "Complete", callback_data=f"reminder_complete:{reminder_id}"
            ),
        ]
        markup.row(buttons[0], buttons[1])
        markup.row(buttons[2], buttons[3])
        markup.row(buttons[4])
        return markup