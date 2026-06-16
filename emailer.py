"""
emailer.py — Optional booking-confirmation email via Brevo.

This is entirely optional (Section 4): the bot works without it. Email is sent
only when ALL of the following hold (the caller enforces this):
  - booking_config.json  send_email_confirmation = true
  - Brevo is fully configured (BREVO_API_KEY, BREVO_SENDER_EMAIL, BREVO_SENDER_NAME)
  - a valid customer email was collected during the conversation

Security:
  - API key from env only; never logged.
  - Recipient email is validated before use (Section 8.3).

CONFIGURATION (optional, see README):
  - BREVO_API_KEY, BREVO_SENDER_EMAIL, BREVO_SENDER_NAME in .env.
"""

import re

import requests

from config import Config
from logger import get_logger

log = get_logger(__name__)

_API_URL = "https://api.brevo.com/v3/smtp/email"
_TIMEOUT = 10  # seconds

# Pragmatic email validation — not RFC-perfect, but rejects obviously bad input.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(email: str) -> bool:
    return bool(email) and bool(_EMAIL_RE.match(email.strip()))


def is_enabled() -> bool:
    """True if Brevo is fully configured and usable."""
    return Config.brevo_enabled()


def send_booking_confirmation(
    to_email: str,
    customer_name: str,
    business_name: str,
    service: str,
    appointment_label: str,
    reference: str,
) -> bool:
    """Send a confirmation email. Returns True on success, False otherwise.

    Never raises — email is best-effort and must not break the booking flow.
    """
    if not is_enabled():
        log.info("Brevo not configured; skipping confirmation email.")
        return False

    if not is_valid_email(to_email):
        log.warning("Invalid recipient email; skipping confirmation email.")
        return False

    safe_name = (customer_name or "there").strip()
    subject = f"Your booking with {business_name} is confirmed ({reference})"
    html = (
        f"<p>Hi {safe_name},</p>"
        f"<p>Your appointment with <strong>{business_name}</strong> is confirmed.</p>"
        f"<ul>"
        f"<li><strong>Service:</strong> {service}</li>"
        f"<li><strong>When:</strong> {appointment_label}</li>"
        f"<li><strong>Booking reference:</strong> {reference}</li>"
        f"</ul>"
        f"<p>If you need to change or cancel, please contact {business_name} directly.</p>"
        f"<p>See you soon!</p>"
    )

    payload = {
        "sender": {
            "name": Config.BREVO_SENDER_NAME,
            "email": Config.BREVO_SENDER_EMAIL,
        },
        "to": [{"email": to_email.strip(), "name": safe_name}],
        "subject": subject,
        "htmlContent": html,
    }

    try:
        resp = requests.post(
            _API_URL,
            json=payload,
            headers={
                "api-key": Config.BREVO_API_KEY,
                "accept": "application/json",
                "content-type": "application/json",
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code not in (200, 201, 202):
            log.error("Brevo send failed: HTTP %s", resp.status_code)
            return False
        log.info("Confirmation email sent ref=%s", reference)
        return True
    except requests.RequestException as exc:
        log.error("Brevo request error: %s", exc)
        return False
