import logging
from typing import Optional, Dict, Any

import telebot


logger = logging.getLogger(__name__)


class TelegramBot:
    """
    Thin wrapper around telebot.TeleBot.

    Externally it provides the SAME interface we used before:
      - send_message(chat_id, text, reply_markup=None)
      - is_allowed_user(update_dict)

    Internally it uses telebot instead of raw requests.
    """

    def __init__(self, token: str, allowed_user_id: Optional[int]):
        self.allowed_user_id = allowed_user_id
        # We won't use telebot's polling or decorator system; just its client.
        self.bot = telebot.TeleBot(token, parse_mode=None)

    def send_message(self,chat_id: int,text: str,reply_markup: Optional[Any] = None,):
        """
        Send a message via Telegram.

        reply_markup can be:
          - None
          - a telebot.types.ReplyKeyboardMarkup / InlineKeyboardMarkup, etc.
        We don't need to care about its exact type here; telebot handles it.
        """
        try:
            msg = self.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
            )
            return msg  # telebot Message object; caller currently doesn't use it
        except Exception:
            logger.exception("Error sending Telegram message")
            raise

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