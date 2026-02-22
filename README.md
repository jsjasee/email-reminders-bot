# Telegram Email Reminders Bot

## 1. Executive Summary
Telegram Email Reminders Bot is a Flask-based webhook service that connects Gmail, Telegram, and Google Sheets to help you manage follow-up reminders from incoming emails. When Gmail push notifications arrive, the app filters senders, sends actionable Telegram cards, and lets you create reminders with one tap.

The goal is to reduce missed follow-ups. Instead of manually tracking important emails, reminders are stored in a Google Sheet, dispatched when due, and controlled directly from Telegram (snooze, custom reschedule, complete).

## 2. Architecture Overview

### High-Level Components
- `app.py`: Main Flask app, routes, webhook handling, reminder workflows, and dispatch logic.
- `config.py`: Environment-driven settings loader, sender allowlist parsing.
- `telegram_bot.py`: Thin Telegram client wrapper and inline keyboard builders.
- `gmail_client.py`: Gmail API client (OAuth token JSON), metadata extraction, history polling.
- `sheets_repo.py`: Google Sheets-backed repository for reminder CRUD and config persistence.
- `date_utils.py`: Working-day date helper (`N` weekdays forward at 9:00 AM).
- `gmail_oauth_setup.py`: One-time local OAuth helper to generate `creds/gmail_token.json`.

### Data Flow (Text Diagram)
```text
Gmail Inbox
   |
   v
Gmail Push (Pub/Sub) --> /gmail-webhook (Flask)
   |                         |
   |                         +--> Gmail history diff -> fetch metadata
   |                         +--> sender allowlist filter
   |                         +--> Telegram "Set reminder / Done" card
   |
Telegram user clicks button (/telegram-webhook)
   |
   +--> create/update/delete reminder in Google Sheets
   |
Scheduled trigger --> /dispatch-due-reminders
   |
   +--> load pending due reminders from Sheets
   +--> send Telegram reminder card (+snooze/custom/complete)
```

### Core Runtime Flow
1. App boots and loads settings from `.env`.
2. Telegram webhook is set using `WEBHOOK_URL`.
3. Gmail webhook receives Pub/Sub notifications and tracks `last_history_id`.
4. New matched emails are sent to Telegram with action buttons.
5. Reminder rows are stored in the `Reminders` worksheet.
6. A scheduler calls `/dispatch-due-reminders` to send due reminders.

## 3. Setup Guide

### Prerequisites
- Python 3.11+
- Telegram bot token (from BotFather)
- Telegram user ID allowed to operate the bot
- Google Cloud project with:
  - Gmail API enabled
  - OAuth Desktop client credentials
  - Pub/Sub topic + Gmail watch permissions
  - Service account with Google Sheets access
- A Google Spreadsheet shared with the service account

### Install
```bash
git clone <your-repo-url>
cd telegram_email_reminders_bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure Environment
Create `.env`:

```env
# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_USER_ID=123456789
WEBHOOK_URL=https://your-domain.com/telegram-webhook

# Google Sheets
GOOGLE_SHEETS_SPREADSHEET_ID=...
GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account",...}

# Gmail
GMAIL_OAUTH_TOKEN_JSON={"token":"...","refresh_token":"...",...}
GMAIL_USER_ID=me
ALLOWED_SENDER_EMAILS=sender1@example.com
sender2@example.com
TARGET_RECIPIENT_EMAIL=your_email@example.com

# App
APP_TIMEZONE=Asia/Singapore
```

Notes:
- `ALLOWED_SENDER_EMAILS` is newline-delimited.
- `TARGET_SENDER_EMAIL` exists as a deprecated fallback.
- `WEBHOOK_URL` must be the full Telegram webhook endpoint.

### Generate Gmail OAuth Token (One-Time)
```bash
python gmail_oauth_setup.py
```
This writes `creds/gmail_token.json`. Use its JSON content for `GMAIL_OAUTH_TOKEN_JSON`.

### Run Locally
Option A:
```bash
FLASK_APP=app:create_app FLASK_ENV=development flask run --reload
```

Option B:
```bash
python app.py
```

Health check:
```bash
curl http://localhost:5001/health
```

## 4. Usage Guide

### Telegram Commands
- `/start`: welcome message.
- `/new`: create a manual reminder.
  - Step 1: send description.
  - Step 2: choose offset (`+1h`, `+1d`, `+3d`, `+1w`, working-day presets, or custom datetime).

### Email Reminder Flow
1. Gmail push arrives at `/gmail-webhook`.
2. App checks sender against allowlist.
3. Telegram message sent:
   - `Set reminder`
   - `Done`
4. If `Set reminder`, choose timing preset or custom datetime.

### Due Reminder Controls
Dispatched reminder cards include:
- `+1 Working Day 9AM`
- `+2 Working Days 9AM`
- `+3 Working Days 9AM`
- `+1 hour`, `+1 day`, `+3 days`, `+1 week`
- `Custom`
- `Complete`

### Useful HTTP Endpoints
| Endpoint | Method | Purpose |
|---|---|---|
| `/` | `GET` | Service liveness text |
| `/health` | `GET` | JSON health/config status |
| `/telegram-webhook` | `POST` | Telegram update receiver |
| `/gmail-webhook` | `POST` | Gmail Pub/Sub receiver |
| `/dispatch-due-reminders` | `GET/POST` | Send due reminders |
| `/test-create-reminder` | `POST` | Dev helper: create test reminder |
| `/test-list-reminders` | `GET` | Dev helper: list reminders |
| `/test-email-notification` | `GET/POST` | Dev helper: send latest email card |
| `/debug/setup-gmail-watch` | `POST` | Dev helper: call Gmail watch |
| `/test-gmail-history` | `GET` | Dev helper: inspect Gmail history |

Example:
```bash
curl -X POST http://localhost:5001/dispatch-due-reminders
```

## 5. Configuration

### Environment Variables
| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token |
| `TELEGRAM_USER_ID` | Recommended | Restrict bot usage to one user |
| `WEBHOOK_URL` | Yes | Full Telegram webhook URL |
| `GOOGLE_SHEETS_SPREADSHEET_ID` | Yes | Spreadsheet ID storing reminders/config |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Yes | Service account JSON content (single-line JSON string) |
| `GMAIL_OAUTH_TOKEN_JSON` | Yes (for Gmail features) | OAuth token JSON content |
| `GMAIL_USER_ID` | Optional | Gmail API user id (`me` default) |
| `ALLOWED_SENDER_EMAILS` | Yes | Newline-separated sender allowlist |
| `TARGET_SENDER_EMAIL` | Deprecated fallback | Old single-sender config |
| `TARGET_RECIPIENT_EMAIL` | Recommended | Recipient matching helper in metadata parsing |
| `APP_TIMEZONE` | Optional | App timezone (`Asia/Singapore` default) |

### Google Sheet Schema
`sheets_repo.py` expects worksheet `Reminders` with headers:
- `reminder_id`
- `source_type`
- `gmail_message_id`
- `subject`
- `sender`
- `recipient`
- `description`
- `telegram_chat_id`
- `due_at`
- `status`

It also uses a `Config` worksheet (`key`, `value`) for `last_history_id`.

### Secrets Handling
- Do not commit `.env` or `creds/` (already ignored in `.gitignore`).
- Keep JSON secrets in environment variables in production.
- Rotate Telegram/Google credentials if exposed.

## 6. Testing

### Current Test Status
No committed automated test suite yet.

### Manual Verification Steps
1. `GET /health` returns `status=ok`.
2. Send `/new` in Telegram and create a reminder.
3. Call `/test-list-reminders` and verify reminder row appears.
4. Call `/dispatch-due-reminders` and verify due reminders are delivered.
5. Call `/test-email-notification` to validate Gmail->Telegram card flow.

### Recommended Automated Tests
- `tests/test_config.py`: allowlist parsing and validation.
- `tests/test_date_utils.py`: working-day calculations.
- `tests/test_sheets_repo.py`: reminder row mapping and status transitions.
- `tests/test_gmail_client.py`: metadata extraction helpers.

Suggested command (after adding tests):
```bash
pytest -q
```

## 7. Deployment

### Local/Dev
- Run Flask locally and expose HTTPS endpoint (for Telegram/Gmail webhooks) via tunnel.
- Set `WEBHOOK_URL` to the public `/telegram-webhook` URL.

### Production (Render)
Build:
```bash
pip install -r requirements.txt
```

Start:
```bash
gunicorn 'app:create_app()' --bind 0.0.0.0:8000
```

### Scheduler
Set up an external cron/job to call `/dispatch-due-reminders` periodically (for example every minute).

### CI/CD Notes
- No CI config is currently committed.
- Minimum CI checks to add:
  - dependency install
  - import/compile check
  - `pytest -q` (once tests are present)

## 8. Contributing Guide

### Branching and Commits
- Work on short-lived feature branches.
- Use concise present-tense commit messages (example: `add sender allowlist parsing`).

### Pull Request Checklist
1. Describe behavior changes clearly.
2. Include rollout notes (new env vars, webhook changes, Sheets schema impact).
3. Provide test evidence (manual endpoint checks or pytest output).
4. Include Telegram screenshots if callback UI text/flows changed.

### Local Quality Gate
```bash
python -m py_compile app.py config.py telegram_bot.py sheets_repo.py gmail_client.py
```

## 9. FAQ & Troubleshooting

### Bot does not receive Telegram updates
- Check `WEBHOOK_URL` points to `/telegram-webhook`.
- Verify endpoint is publicly reachable over HTTPS.
- Confirm bot token is valid.

### `No allowed sender emails configured`
- Set `ALLOWED_SENDER_EMAILS` in `.env`.
- Use one sender email per line.

### Gmail webhook receives events but no Telegram messages are sent
- Confirm sender is in `ALLOWED_SENDER_EMAILS`.
- Check `TELEGRAM_USER_ID` and bot config.
- Ensure `last_history_id` exists in `Config` sheet after first bootstrap call.

### Google Sheets errors (auth/permission/not found)
- Verify `GOOGLE_SERVICE_ACCOUNT_JSON` is valid JSON.
- Share spreadsheet with service account email.
- Verify `GOOGLE_SHEETS_SPREADSHEET_ID` is correct.

### Reminders not dispatching
- Ensure reminders are `status="pending"` and `due_at <= now`.
- Ensure scheduler calls `/dispatch-due-reminders`.
- Verify timezone (`APP_TIMEZONE`) is correct.

### `GMAIL_OAUTH_TOKEN_JSON` errors
- Re-run `python gmail_oauth_setup.py`.
- Replace env var with the full JSON content from `creds/gmail_token.json`.

## 10. License and Credits

### License
No `LICENSE` file is currently present in this repository. Add one before public release.

### Credits
- Flask
- pyTelegramBotAPI
- Google Gmail API
- Google Sheets API (`gspread`)
- python-dotenv
