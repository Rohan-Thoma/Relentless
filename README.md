# Relentless

Single-user, human-confirmed job-search agent.

## Setup
1. Create Python 3.11 venv, install deps: `pip install -r requirements-dev.txt`
2. Copy `.env.example` to `.env` and fill credentials.
3. Run migrations: `python -m src.db_migrate`
4. Sync profile: `python manage.py sync`
5. Doctor: `python manage.py doctor`

## Run
`python manage.py run`

## Safety
- Telegram updates are restricted to `TELEGRAM_CHAT_ID`.
- Destructive commands require confirmation.
- No application is submitted without a confirmation token.