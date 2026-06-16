# Appointment Booking Bot

A standalone, configurable **WhatsApp appointment booking agent** for service
businesses — clinics, salons, tutors, consultants, lawyers. A customer messages
your WhatsApp number, the bot shows available slots, books the appointment into
Notion, notifies you on Telegram, and sends 24-hour and 2-hour reminders.

Clone it, configure it, deploy it. No code changes needed for normal use — all
business settings live in `booking_config.json` and `.env`.

> Built by **Jorion Technologies**. This is the Tier 1 (simple) edition.

---

## 1. What it does

```
Customer → WhatsApp → Bot
  1. Greets the customer, asks which service they want
  2. Shows the next available time slots (generated from your hours/config)
  3. Customer picks a slot
  4. Bot collects the customer's name (phone is already known from WhatsApp)
  5. Confirms and writes the booking to Notion
  6. Notifies you (the owner) on Telegram
  7. Sends the customer a WhatsApp confirmation with a booking reference
  8. Later: sends a 24-hour and a 2-hour reminder before the appointment
```

The bot uses Claude **only** to understand fuzzy replies (e.g. "a cleaning
please", "the second one", "11:30 works"). All messages sent to customers are
fixed templates — predictable and safe.

---

## 2. Prerequisites

- **Python 3.11+**
- A **Twilio** account — for WhatsApp (the free Sandbox works for testing)
- A **Notion** account — stores your bookings
- A **Telegram** account — receives owner notifications
- *(Optional)* a **Brevo** account — for confirmation emails
- A way to expose a **public HTTPS URL** for the webhook (e.g. **ngrok** for
  testing, or a reverse proxy / tunnel in production)

---

## 3. Quick start

```bash
git clone <your-repo-url> appointment-booking-bot
cd appointment-booking-bot

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # then fill in real values (Sections 4–7)
#  edit booking_config.json        # your services, hours, timezone (Section 8)
```

Then run the app (Section 9) and schedule reminders (Section 10).

> ⚠️ **Never commit your `.env`.** It is already in `.gitignore`. Only
> `.env.example` (placeholders) belongs in git.

---

## 4. Twilio WhatsApp setup (the hard part — do this carefully)

This build uses **Twilio WhatsApp**. The free **Sandbox** is perfect for testing.

1. **Create a Twilio account** at <https://www.twilio.com/> (free trial).
2. **Activate the WhatsApp Sandbox**: in the Console, go to
   *Messaging → Try it out → Send a WhatsApp message*.
3. **Join the sandbox**: from your own phone, send the given join code (e.g.
   `join <two-words>`) as a WhatsApp message to the Twilio sandbox number shown
   on that page. (Anyone who wants to message your bot must join the sandbox
   first — see the limitations note below.)
4. **Get your credentials** from the Console dashboard:
   - **Account SID** → `TWILIO_ACCOUNT_SID`
   - **Auth Token** → `TWILIO_AUTH_TOKEN` — treat this like a password. If it is
     ever exposed, rotate it immediately in the Console.
   - **Sandbox number** → `TWILIO_WHATSAPP_FROM`, in the form
     `whatsapp:+14155238886` (keep the `whatsapp:` prefix).
5. **Expose your webhook over HTTPS**:
   - Local testing: start a tunnel, e.g. `ngrok http 5001` (or `cloudflared`),
     and copy the `https://…` URL it gives you.
   - Production: use your server's public HTTPS domain.
6. **Configure the webhook in Twilio**: *Messaging → Try it out → WhatsApp
   Sandbox Settings*. In **"When a message comes in"**, paste your full webhook
   URL `https://<your-public-url>/whatsapp/webhook` and set the method to
   **POST**. Save.
7. **Set `PUBLIC_WEBHOOK_URL`** in `.env` to that **exact same URL**. The bot
   validates Twilio's request signature against this URL, so it must match the
   Console value character-for-character (scheme, host, and path).
8. **Copy `.env.example` to `.env`** and fill in every value.

> **Sandbox limitations** (document these for go-live):
> - Anyone messaging your bot must first join the sandbox with the join code.
> - The sandbox session expires after **72 hours** of inactivity (the user
>   re-joins to continue).
> - For production you move to an **approved WhatsApp sender** (your own number,
>   approved by Meta via Twilio) — this is the Tier 2 / go-live step, available
>   from Jorion Technologies.

---

## 5. Notion setup

1. Create a **new database** (a full-page table) in Notion, e.g. "Appointments".
2. Add these properties **with these exact names and types**:

   | Property | Type | Notes |
   |---|---|---|
   | `Customer Name` | Title | (the default title column — rename it) |
   | `Phone / WhatsApp` | Phone | |
   | `Service` | Select | Add an option per service you offer |
   | `Appointment Date` | Date | Enable **"Include time"** |
   | `Status` | Select | Options: `Booked`, `Reminded-24h`, `Reminded-2h`, `Completed`, `Cancelled`, `No-Show` |
   | `Booking Reference` | Text (Rich text) | |
   | `Created At` | Date | Enable **"Include time"** |
   | `Reminder 24h Sent` | Checkbox | |
   | `Reminder 2h Sent` | Checkbox | |
   | `Notes` | Text (Rich text) | |

3. Create an **integration**: <https://www.notion.so/my-integrations> →
   *New integration* → copy the **Internal Integration Secret** → this is your
   **`NOTION_TOKEN`**.
4. **Share the database with the integration**: open the database → *⋯ menu →
   Connections → Connect to →* your integration.
5. **Get the database ID**: open the database as a full page; the URL looks like
   `https://www.notion.so/<workspace>/<DATABASE_ID>?v=...`. The 32-character
   hex string is your **`NOTION_BOOKINGS_DB_ID`**.

---

## 6. Telegram setup (owner notifications)

1. In Telegram, message **@BotFather** → `/newbot` → follow the prompts → copy the
   **bot token** → this is your **`TELEGRAM_BOT_TOKEN`**.
2. **Get your chat ID**:
   - Send any message to your new bot first (so it can message you back).
   - Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser.
   - Find `"chat":{"id":<number>}` → that number is your
     **`TELEGRAM_ADMIN_CHAT_ID`**.

---

## 7. Brevo email confirmations (optional)

The bot works fine without this. To enable confirmation emails:

1. Create a Brevo account → **SMTP & API → API Keys** → create a key →
   **`BREVO_API_KEY`**.
2. Verify a sender email/domain in Brevo → set **`BREVO_SENDER_EMAIL`** and
   **`BREVO_SENDER_NAME`**.
3. Set `"send_email_confirmation": true` in `booking_config.json`.

> Note: the Tier 1 conversation flow collects name + phone only. Email
> confirmation fires only if an email is also collected; capturing email is a
> small extension point in `conversation.py`.

---

## 8. Configure your business (`booking_config.json`)

Edit this file to match your business — no code changes needed:

```json
{
  "business_name": "Bright Smile Dental",
  "timezone": "Africa/Lagos",
  "booking_window_days": 7,
  "slot_duration_minutes": 30,
  "working_hours": {
    "monday":    {"open": "09:00", "close": "17:00"},
    "tuesday":   {"open": "09:00", "close": "17:00"},
    "wednesday": {"open": "09:00", "close": "17:00"},
    "thursday":  {"open": "09:00", "close": "17:00"},
    "friday":    {"open": "09:00", "close": "16:00"},
    "saturday":  {"open": "10:00", "close": "14:00"},
    "sunday":    null
  },
  "break_times": [
    {"start": "13:00", "end": "14:00"}
  ],
  "services": [
    {"name": "Consultation", "duration_minutes": 30},
    {"name": "Cleaning", "duration_minutes": 60},
    {"name": "Check-up", "duration_minutes": 30}
  ],
  "max_bookings_per_slot": 1,
  "send_email_confirmation": false
}
```

- **`timezone`** — an IANA name (e.g. `Africa/Lagos`, `Europe/London`,
  `America/New_York`). All slots and reminders use this.
- **`working_hours`** — set a day to `null` to mark it closed.
- **`break_times`** — slots overlapping a break are skipped.
- **`services`** — each service's `duration_minutes` controls how long it blocks.
- The `Service` select options in Notion must match these service names.

---

## 9. Run the app

```bash
source .venv/bin/activate
python app.py
```

The webhook needs to be reachable over **public HTTPS**.

- **Local testing** — expose your local port with ngrok:
  ```bash
  ngrok http 5001
  ```
  Use the `https://…ngrok…/whatsapp/webhook` URL as your webhook in the Twilio
  Sandbox settings (Section 4.6), and set `PUBLIC_WEBHOOK_URL` in `.env` to that
  exact same URL (Section 4.7).
- **Production** — run behind a reverse proxy / tunnel that terminates HTTPS
  (nginx, Caddy, Cloudflare Tunnel, etc.) and a WSGI server (gunicorn/uwsgi), and
  keep the process alive with a process manager (systemd, supervisor, pm2,
  Docker, …).

The app also exposes **`GET /health`** (returns `{"status": "ok"}`) for uptime
checks.

**Production safety**: keep `FLASK_ENV=production` in `.env`. This disables Flask
debug mode and stack traces. Never run with `FLASK_ENV=development` in production.

---

## 10. Schedule reminders (`reminders.py`)

`reminders.py` is a standalone script — run it every **15–30 minutes**. It sends
24h and 2h reminders and uses Notion checkboxes so nobody gets a duplicate.

**Linux/macOS (cron)** — `crontab -e`:

```cron
*/15 * * * * cd /path/to/appointment-booking-bot && /path/to/.venv/bin/python reminders.py >> logs/reminders.cron.log 2>&1
```

**Windows (Task Scheduler)**: create a task that runs every 15 minutes:
`Program: C:\path\to\.venv\Scripts\python.exe`,
`Arguments: reminders.py`,
`Start in: C:\path\to\appointment-booking-bot`.

---

## 11. Project layout

```
config.py            Loads + validates env vars (fails loud at startup)
logger.py            Rotating logs with PII redaction
booking_config.json  Your business settings
slots.py             Slot generation + availability
notion_manager.py    Bookings read/write
nlu.py               Claude intent parsing (injection-guarded)
notifier.py          Telegram owner notifications
emailer.py           Optional Brevo confirmation emails
conversation.py      The booking state machine
whatsapp.py          Twilio WhatsApp send wrapper
app.py               Flask webhook (Twilio signature validation + idempotency)
reminders.py         Scheduled 24h / 2h reminders
```

---

## 12. Security notes

This bot is built to a strict security standard:

- All secrets come from environment variables; `.env` is gitignored.
- Every inbound webhook is verified with **Twilio's `X-Twilio-Signature`**
  (HMAC-SHA1 over the request URL + parameters, keyed by your Auth Token). This
  always runs — there is **no sandbox bypass**. Mismatches are rejected with 403.
- Duplicate deliveries are de-duplicated by `MessageSid` (idempotency), so a
  Twilio retry never creates a double booking.
- Every external input is validated and sanitized before use.
- Customer messages sent to Claude are treated as untrusted: a strict system
  prompt + output validation block prompt-injection attempts.
- **Logs never contain tokens or full phone numbers** — phones are redacted
  (`+234***890`) and bookings are traced by reference code.
- Per-number rate limiting prevents spam/abuse loops.

---

## 13. Troubleshooting

- **Bot won't start / "missing required configuration"** — a required `.env`
  value is empty. The startup log lists exactly which ones.
- **Inbound messages rejected with 403** — `PUBLIC_WEBHOOK_URL` in `.env` must
  match the webhook URL in the Twilio Sandbox settings **exactly** (scheme, host,
  and path). The signature is computed over that URL, so any mismatch fails.
- **Bot never replies in WhatsApp** — confirm you've joined the sandbox (and that
  it hasn't expired after 72h), the webhook URL is reachable over HTTPS, and the
  Twilio credentials in `.env` are correct.
- **No bookings appear in Notion** — confirm the database is shared with your
  integration, the property names/types match Section 5 exactly, and
  `NOTION_BOOKINGS_DB_ID` is correct.
- **No reminders** — check that `reminders.py` is actually scheduled and that the
  appointment times and timezone in `booking_config.json` are correct.

---

## 14. Tier 2 upgrade path

The following are **out of scope** for Tier 1 and available as paid upgrades from
Jorion Technologies:

- 💳 Payment collection
- 📅 Google Calendar / Outlook sync
- 🔁 Cancellation & reschedule via the bot
- 👥 Multiple staff / practitioners
- 🧠 Persistent conversation state across restarts (Redis)
- 🌍 Multi-language support

→ <https://joriontech.com/ai-agents>

---

## 15. License & attribution

Built by **Jorion Technologies**. See repository license for terms.
