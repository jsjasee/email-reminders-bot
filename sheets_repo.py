import json
import logging
from datetime import datetime, timezone
from typing import Any, List

import gspread
from google.oauth2.service_account import Credentials


logger = logging.getLogger(__name__)

# Only need Sheets scope for this app
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class ReminderSheetRepository:
    """
    Thin wrapper around a single Google Sheet used as our 'DB'.

    For now:
      - Connect to spreadsheet
      - Ensure a 'Reminders' worksheet exists
      - Provide simple test methods to append + read rows
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
            # Create a new worksheet with some default size
            worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=1000, cols=10)

        self.worksheet = worksheet

    # --------- TEMP TEST METHODS (connectivity only) --------- #

    def append_test_row(self, note: str) -> None:
        """
        Append a very simple row: [timestamp_utc, note].
        Just to verify connectivity.
        """
        now = datetime.now(timezone.utc).isoformat()
        self.worksheet.append_row([now, note])

    def get_all_values(self) -> List[List[Any]]:
        """
        Return all values in the worksheet as a 2D list.
        """
        return self.worksheet.get_all_values()