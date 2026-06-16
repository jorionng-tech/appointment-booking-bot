"""
notifier.py — Admin notifications via Telegram.

Used for:
  - New booking alerts to the business owner.
  - Escalations when a customer says something off-flow (Section 7).
  - Fail-loud error alerts (Build Principle 4) when something breaks.

Security:
  - Telegram bot token comes from env only; never logged.
  - Messages to admin MAY contain customer details (the admin is the business
    owner and needs them), but our own logs still redact phones (Section 8.6).

CONFIGURATION REQUIRED (see README):
  - TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_CHAT_ID in .env.
The bot degrades safely: if Telegram is unreachable, we log the failure and
return False rather than crashing the booking flow.
"""

import requests

from config import Config
from logger import get_logger, redact_phone

log = get_logger(__name__)

_API_BASE = "https://api.telegram.org"
_TIMEOUT = 10  # seconds


def _send(text: str) -> bool:
    """Low-level send to the admin chat. Returns True on success."""
    token = Config.TELEGRAM_BOT_TOKEN
    chat_id = Config.TELEGRAM_ADMIN_CHAT_ID
    if not token or not chat_id:
        log.error("Telegram not configured; cannot notify admin.")
        return False

    url = f"{_API_BASE}/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            # resp.text can echo the chat_id but never the token (token is in URL).
            log.error("Telegram sendMessage failed: HTTP %s", resp.status_code)
            return False
        return True
    except requests.RequestException as exc:
        log.error("Telegram request error: %s", exc)
        return False


def _esc(value) -> str:
    """Escape the few characters that matter for Telegram HTML parse mode."""
    s = "" if value is None else str(value)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def notify_new_booking(
    business_name: str,
    customer_name: str,
    phone: str,
    service: str,
    appointment_label: str,
    reference: str,
) -> bool:
    """Tell the admin a new booking was made."""
    text = (
        f"🗓️ <b>New booking</b> — {_esc(business_name)}\n\n"
        f"<b>Ref:</b> {_esc(reference)}\n"
        f"<b>Name:</b> {_esc(customer_name)}\n"
        f"<b>Phone:</b> {_esc(phone)}\n"
        f"<b>Service:</b> {_esc(service)}\n"
        f"<b>When:</b> {_esc(appointment_label)}"
    )
    ok = _send(text)
    # Our own log stays PII-safe: reference + redacted phone only.
    log.info(
        "Admin booking notification %s ref=%s phone=%s",
        "sent" if ok else "FAILED",
        reference,
        redact_phone(phone),
    )
    return ok


def notify_escalation(business_name: str, phone: str, message: str) -> bool:
    """Tell the admin a customer needs human follow-up (off-flow message)."""
    text = (
        f"🙋 <b>Customer needs follow-up</b> — {_esc(business_name)}\n\n"
        f"<b>Phone:</b> {_esc(phone)}\n"
        f"<b>Message:</b> {_esc(message)}"
    )
    ok = _send(text)
    log.info(
        "Admin escalation %s phone=%s",
        "sent" if ok else "FAILED",
        redact_phone(phone),
    )
    return ok


def notify_error(context: str, detail: str = "") -> bool:
    """Fail-loud alert to admin when something breaks (Build Principle 4)."""
    text = (
        "⚠️ <b>Appointment bot error</b>\n\n"
        f"<b>Where:</b> {_esc(context)}\n"
        f"<b>Detail:</b> {_esc(detail)[:500]}"
    )
    ok = _send(text)
    log.info("Admin error alert %s context=%s", "sent" if ok else "FAILED", context)
    return ok


def notify_reminder_sent(reference: str, kind: str, appointment_label: str) -> bool:
    """Optional: confirm to admin that a reminder went out (used by reminders.py)."""
    text = (
        f"⏰ <b>{_esc(kind)} reminder sent</b>\n\n"
        f"<b>Ref:</b> {_esc(reference)}\n"
        f"<b>When:</b> {_esc(appointment_label)}"
    )
    return _send(text)
