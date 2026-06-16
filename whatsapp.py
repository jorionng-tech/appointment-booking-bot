"""
whatsapp.py — Twilio WhatsApp send wrapper.

Provides the single outbound primitive the rest of the bot depends on:

    send_text(phone: str, text: str) -> bool

`reminders.py` and `app.py` both rely on this exact signature and on it NEVER
raising — it returns True on success and False on any failure.

Provider: Twilio WhatsApp. Messages are sent via an authenticated form-encoded
POST to the Twilio REST API (no SDK required — plain `requests` keeps the
dependency surface small and the code auditable).

Security (Section 8.5 / 8.6 / Addendum 3):
  - Credentials come from env only (Config); the Auth Token is NEVER logged.
  - Recipient phone numbers are redacted in logs (+234***890). The logger's
    RedactingFilter also scrubs phone-like strings from any Twilio response
    body we log, as defence in depth.

CONFIGURATION REQUIRED (see README):
  - TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM in .env.
"""

import re

import requests

from config import Config
from logger import get_logger, redact_phone

log = get_logger(__name__)

_TIMEOUT = 10  # seconds
_API_BASE = "https://api.twilio.com/2010-04-01"

# Minimum sensible digit count for an international number.
_MIN_DIGITS = 7
_MAX_DIGITS = 15  # E.164 maximum


def _normalize_to_whatsapp(phone: str):
    """Normalise a recipient to Twilio's 'whatsapp:+<digits>' form.

    Accepts input with or without a 'whatsapp:' prefix and with or without a
    leading '+'. Returns the normalised string, or None if the number is
    obviously invalid (too short/long or non-numeric after cleaning).
    """
    if not phone:
        return None
    raw = str(phone).strip()
    # Drop a leading 'whatsapp:' if the caller already added it.
    if raw.lower().startswith("whatsapp:"):
        raw = raw[len("whatsapp:"):]
    # Keep digits only (this also drops '+', spaces, and dashes).
    digits = re.sub(r"\D", "", raw)
    if not (_MIN_DIGITS <= len(digits) <= _MAX_DIGITS):
        return None
    return f"whatsapp:+{digits}"


def send_text(phone: str, text: str) -> bool:
    """Send a WhatsApp message via Twilio.

    `phone` is the recipient in E.164 (e.g. +2348012345678); this function adds
    the 'whatsapp:' prefix itself. Returns True on success, False on failure.
    Never raises — callers depend on a bool.
    """
    account_sid = Config.TWILIO_ACCOUNT_SID
    auth_token = Config.TWILIO_AUTH_TOKEN
    from_addr = Config.TWILIO_WHATSAPP_FROM

    if not account_sid or not auth_token or not from_addr:
        log.error("Twilio not configured; cannot send WhatsApp message.")
        return False

    to_addr = _normalize_to_whatsapp(phone)
    if to_addr is None:
        log.error("Invalid recipient number; not sending. phone=%s", redact_phone(phone))
        return False

    if not text:
        log.warning("Empty message body; not sending. phone=%s", redact_phone(phone))
        return False

    url = f"{_API_BASE}/Accounts/{account_sid}/Messages.json"
    try:
        resp = requests.post(
            url,
            data={"From": from_addr, "To": to_addr, "Body": text},
            auth=(account_sid, auth_token),  # HTTP Basic — token never logged
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.error("Twilio send request error phone=%s: %s", redact_phone(phone), exc)
        return False

    if resp.status_code // 100 != 2:
        # Body may echo the recipient number; the RedactingFilter scrubs it, and
        # the Auth Token is in the header (never in the body), so this is safe.
        log.error(
            "Twilio send failed phone=%s: HTTP %s %s",
            redact_phone(phone),
            resp.status_code,
            (resp.text or "")[:300],
        )
        return False

    log.info("WhatsApp message sent phone=%s", redact_phone(phone))
    return True
