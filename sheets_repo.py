import json
import logging
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Any, List, Optional

import gspread
from google.oauth2.service_account import Credentials


logger = logging.getLogger(__name__)

# Only need Sheets scope for this app
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Our Reminders table schema (header row)
REMINDER_HEADERS = [
    "reminder_id",
    "source_type",        # "email" or "manual"
    "gmail_message_id",   # email reminders only; empty for manual
    "subject",            # email subject
    "sender",             # email sender
    "recipient",          # email recipient
    "description",        # manual reminders: free text
    "telegram_chat_id",
    "due_at",             # ISO datetime (Asia/Singapore)
    "status",             # "pending" etc.
]


@dataclass
class Reminder:
    reminder_id: str
    source_type: str
    gmail_message_id: Optional[str]
    subject: Optional[str]
    sender: Optional[str]
    recipient: Optional[str]
    description: Optional[str]
    telegram_chat_id: int
    due_at: datetime
    status: str
    # Internal: which row this reminder lives on (for updates/deletes)
    row_number: Optional[int] = None


class ReminderSheetRepository:
    """
    Wrapper around a single Google Sheet worksheet used as our 'DB'.

    Responsibilities:
      - Ensure a worksheet named 'Reminders' exists with the right header row.
      - Map between worksheet rows and Reminder objects.
      - Provide CRUD-style methods for reminders.
    """

    def __init__(self, spreadsheet_id: str, service_account_json: str, worksheet_name: str = "Reminders"):
        self.spreadsheet_id = spreadsheet_id
        self.worksheet_name = worksheet_name

        # Parse service account JSON from env string
        info = json.loads(service_account_json)

        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        client = gspread.authorize(creds)

        spreadsheet = client.open_by_key(spreadsheet_id)

        try:
            worksheet = spreadsheet.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            logger.info("Worksheet '%s' not found, creating it.", worksheet_name)
            worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=1000, cols=20)

        self.worksheet = worksheet

        # Ensure header row is present and correct
        self._ensure_header()

    def _ensure_header(self) -> None:
        """
        Make sure the first row contains REMINDER_HEADERS.

        If the sheet is empty, write the header.
        If the first row differs, overwrite it (we don't care about old test data).
        """
        values = self.worksheet.get_all_values()
        if not values:
            logger.info("Worksheet is empty, writing header row.")
            self.worksheet.append_row(REMINDER_HEADERS)
            return

        first_row = values[0]
        if first_row != REMINDER_HEADERS:
            logger.warning("First row does not match expected headers, overwriting it.")
            # Overwrite row 1 with our headers
            self.worksheet.update("1:1", [REMINDER_HEADERS])

    # --------- TEST METHODS (still useful) --------- #

    def append_test_row(self, note: str) -> None:
        """
        Append a very simple row after the header: [timestamp_utc, note].
        Only used by /test-sheets; safe to keep around.
        """
        now = datetime.now(UTC).isoformat()
        # We'll just append in the first two columns after the header.
        self.worksheet.append_row([now, note])

    def get_all_values(self) -> List[List[Any]]:
        """Return all values in the worksheet as a 2D list."""
        return self.worksheet.get_all_values()

    # --------- REMINDER CRUD API --------- #

    def create_reminder(self, reminder: Reminder) -> None:
        """
        Append a new reminder row to the sheet.
        """
        row = [
            reminder.reminder_id,
            reminder.source_type,
            reminder.gmail_message_id or "",
            reminder.subject or "",
            reminder.sender or "",
            reminder.recipient or "",
            reminder.description or "",
            str(reminder.telegram_chat_id),
            reminder.due_at.isoformat(),
            reminder.status,
        ]
        self.worksheet.append_row(row)

    def _row_to_reminder(self, row_dict: dict, row_number: int) -> Optional[Reminder]:
        """
        Convert a dict from get_all_records() into a Reminder.
        Ignore rows that don't have a reminder_id.
        """

        def as_str(value: Any) -> str:
            if value is None:
                return ""
            return str(value)

        reminder_id = as_str(row_dict.get("reminder_id")).strip()
        if not reminder_id:
            return None

        due_at_str = as_str(row_dict.get("due_at")).strip()
        if not due_at_str:
            # Bad / incomplete row, ignore
            return None

        try:
            due_at = datetime.fromisoformat(due_at_str)
        except ValueError:
            logger.warning("Invalid due_at format in row %s: %r", row_number, due_at_str)
            return None

        chat_id_raw = as_str(row_dict.get("telegram_chat_id")).strip()
        try:
            telegram_chat_id = int(chat_id_raw) if chat_id_raw else 0
        except ValueError:
            logger.warning(
                "Invalid telegram_chat_id in row %s: %r",
                row_number,
                row_dict.get("telegram_chat_id"),
            )
            telegram_chat_id = 0

        return Reminder(
            reminder_id=reminder_id,
            source_type=as_str(row_dict.get("source_type")),
            gmail_message_id=as_str(row_dict.get("gmail_message_id")) or None,
            subject=as_str(row_dict.get("subject")) or None,
            sender=as_str(row_dict.get("sender")) or None,
            recipient=as_str(row_dict.get("recipient")) or None,
            description=as_str(row_dict.get("description")) or None,
            telegram_chat_id=telegram_chat_id,
            due_at=due_at,
            status=as_str(row_dict.get("status")),
            row_number=row_number,
        )

    def get_all_reminders(self) -> List[Reminder]:
        """
        Return all reminders as Reminder objects.

        Uses get_all_records(), which returns:
          - A list of dicts, one per row after the header.
        Row number is 2 + index (since header is row 1).
        """
        records = self.worksheet.get_all_records()
        reminders: List[Reminder] = []
        for idx, row_dict in enumerate(records):
            row_number = idx + 2  # header is row 1
            reminder = self._row_to_reminder(row_dict, row_number)
            if reminder is not None:
                reminders.append(reminder)
        return reminders

    def get_due_reminders(self, now: datetime) -> List[Reminder]:
        """
        Return all reminders where:
          - status == "pending"
          - due_at <= now
        """
        all_reminders = self.get_all_reminders()
        due: List[Reminder] = []
        for r in all_reminders:
            if r.status == "pending" and r.due_at <= now:
                due.append(r)
        return due

    def update_reminder_due_at(self, reminder_id: str, new_due_at: datetime) -> bool:
        """
        Set due_at = new_due_at and status = "pending" for the row matching reminder_id.
        Returns True if updated, False if not found.
        """
        reminders = self.get_all_reminders()
        for r in reminders:
            if r.reminder_id == reminder_id and r.row_number is not None:
                # Update due_at
                self.worksheet.update_cell(
                    r.row_number,
                    REMINDER_HEADERS.index("due_at") + 1,
                    new_due_at.isoformat(),
                )
                # Reset status so it will be picked up again at the new due_at
                self.worksheet.update_cell(
                    r.row_number,
                    REMINDER_HEADERS.index("status") + 1,
                    "pending",
                )
                return True
        return False

    def delete_reminder(self, reminder_id: str) -> bool:
        """
        Delete the row matching reminder_id.
        Returns True if a row was deleted, False if not found.
        """
        reminders = self.get_all_reminders()
        for r in reminders:
            if r.reminder_id == reminder_id and r.row_number is not None:
                self.worksheet.delete_rows(r.row_number)
                return True
        return False

    def update_reminder_status(self, reminder_id: str, new_status: str) -> bool:
        """
        Set status = new_status for the row matching reminder_id.
        Returns True if updated, False if not found.
        """
        reminders = self.get_all_reminders()
        for r in reminders:
            if r.reminder_id == reminder_id and r.row_number is not None:
                self.worksheet.update_cell(
                    r.row_number,
                    REMINDER_HEADERS.index("status") + 1, # python counts from 0, but google sheets count from 1, so need to +1 to offset this
                    new_status,
                )
                return True
        return False