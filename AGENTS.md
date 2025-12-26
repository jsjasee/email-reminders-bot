# Repository Guidelines

## Project Structure & Module Organization
The app is a small Flask service (`app.py`) that wires together configuration, the Telegram bot wrapper, the Google Sheets repository, and the Gmail client. Core integration helpers live in dedicated modules: `telegram_bot.py` handles inline keyboards and access control, `sheets_repo.py` maps reminders onto the Reminders worksheet, and `gmail_client.py` fetches metadata from Gmail Push notifications. Configuration is read via `config.py` (dotenv-aware). Credentials downloaded from Google Cloud belong in `creds/` and must stay out of source control. Add new helpers next to their domains rather than bloating `app.py`.

## Build, Test, and Development Commands
- `python -m venv .venv && source .venv/bin/activate` — create an isolated environment (Python 3.11+ recommended).
- `pip install -r requirements.txt` — install Flask, pyTelegramBotAPI, and Google client SDKs.
- `FLASK_APP=app:create_app FLASK_ENV=development flask run --reload` — boot the webhook receiver locally with hot reload.
- `gunicorn 'app:create_app()' --bind 0.0.0.0:8000` — production-style process for Render or Docker.
- `python gmail_oauth_setup.py` — one-time script that generates `creds/gmail_token.json` for OAuth.

## Coding Style & Naming Conventions
Use Black-style 4-space indentation, snake_case for functions/variables, and PascalCase for classes (`ReminderSheetRepository`). Keep type hints and logging consistent with current modules. Inline comments should explain state machines or callback payloads, not obvious assignments. Constants and environment keys stay uppercase. Prefer small helper functions inside routes when parsing Telegram callback payloads.

## Testing Guidelines
Automated tests are not yet committed; new work should add `tests/test_*.py` modules using `pytest` to cover parsing helpers, Sheets adapters, and Gmail metadata extraction. Run `pytest -q` locally before opening a PR. Until more coverage exists, manually trigger `/health`, `/telegram-webhook`, and Gmail watch flows to confirm happy-path behaviour and log cleanliness.

## Commit & Pull Request Guidelines
Follow the existing history: short, present-tense subjects under ~60 characters (e.g., `add recipient data`). Each PR should describe the feature, rollout steps (env vars, webhook URLs), and include screenshots of Telegram interactions when UI elements change. Reference linked issues, call out migrations to Sheets schema, and note any manual Google Cloud or BotFather steps so reviewers can reproduce them.

## Security & Configuration Tips
Never commit `.env`, tokens, or anything in `creds/`. Rotate Google credentials if they touch public history. When testing locally, populate the required env vars (`TELEGRAM_BOT_TOKEN`, `GOOGLE_SHEETS_SPREADSHEET_ID`, `GMAIL_OAUTH_TOKEN_JSON`, etc.) via a `.env` file that mirrors production. Double-check webhook URLs and allowed Telegram user IDs before deploying so reminders remain private.
