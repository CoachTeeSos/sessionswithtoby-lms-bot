# SessionsWithToby — Telegram LMS Bot

Faceless vocal-coaching bot: delivers lessons from the SessionsWithToby LMS
over Telegram, tracks progress, and upsells the full course via Flutterwave
(price auto-set by student country).

## Run locally
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=... FLUTTERWAVE_SECRET_KEY=...
python bot.py
```

## Deploy (Railway, separate free-tier project)
1. Push this folder to a new GitHub repo.
2. New Railway project → Deploy from GitHub → pick repo.
3. Add env vars: TELEGRAM_BOT_TOKEN, FLUTTERWAVE_SECRET_KEY.
4. Deploy. Bot polls Telegram 24/7.

## Data
- lessons.json / courses.json — copied from CoachTeeSos/sessionswithtoby- (main).
- users.json — local user store (swap for Google Sheet later).

## Loop
/start → country → name → email → lesson 1 → "done" advances →
at lesson 3: geo-priced Flutterwave upgrade link.
