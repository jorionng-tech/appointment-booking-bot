"""
logger.py — Rotating file logger with PII redaction.

Security (Section 8.5 / 8.6):
  - Phone numbers are redacted everywhere they appear in log text  (+234***890).
  - Common secret-bearing tokens are redacted if they ever leak into a message.
  - Customer names are NOT logged by callers; log booking references instead.

Usage:
    from logger import get_logger, redact_phone
    log = get_logger(__name__)
    log.info("Booking created ref=%s phone=%s", ref, redact_phone(phone))

The RedactingFilter is a defence-in-depth backstop: even if a caller forgets
to redact, the filter scrubs phone-like and secret-like substrings before they
hit disk or stdout.
"""

import logging
import os
import re
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "appointment_bot.log")

# Max 5 MB per file, keep 5 rotated backups.
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ── Redaction patterns ────────────────────────────────────────────────────
# Phone numbers: optional '+', then 7+ digits (allowing spaces/dashes between).
_PHONE_RE = re.compile(r"\+?\d[\d\-\s]{6,}\d")

# Secret-ish key/value pairs, e.g. token=..., api_key: ..., secret="...".
_SECRET_KV_RE = re.compile(
    r"(?i)\b(token|secret|api[_-]?key|authorization|bearer)\b\s*[=:]\s*\S+"
)


def redact_phone(phone) -> str:
    """Redact a phone number for safe logging: +2348012345890 -> +234***890.

    Keeps the leading country-ish prefix and last 3 digits for traceability,
    masks the middle. Non-string / short inputs are masked entirely.
    """
    if phone is None:
        return "***"
    s = str(phone).strip()
    digits = re.sub(r"\D", "", s)
    if len(digits) < 7:
        return "***"
    prefix = ("+" if s.startswith("+") else "") + digits[:3]
    suffix = digits[-3:]
    return f"{prefix}***{suffix}"


def _scrub_secret(match: "re.Match") -> str:
    """Keep the key name, mask the value: 'token=abc123' -> 'token=***REDACTED***'."""
    raw = match.group(0)
    sep = "=" if "=" in raw else ":"
    key = raw.split(sep, 1)[0].rstrip()
    return f"{key}{sep}***REDACTED***"


def _redact_text(text: str) -> str:
    """Apply secret-key scrubbing first, then phone redaction."""
    scrubbed = _SECRET_KV_RE.sub(_scrub_secret, text)
    return _PHONE_RE.sub(lambda m: redact_phone(m.group(0)), scrubbed)


class RedactingFilter(logging.Filter):
    """Backstop filter that scrubs PII/secrets from every emitted record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            # If formatting fails, let the handler deal with the raw record.
            return True

        # Replace the record's message with a scrubbed, fully-formatted version.
        record.msg = _redact_text(msg)
        record.args = ()
        return True


_configured = False


def _configure_root() -> None:
    """One-time configuration of the 'appointment_bot' logger tree."""
    global _configured
    if _configured:
        return

    os.makedirs(LOG_DIR, exist_ok=True)

    root = logging.getLogger("appointment_bot")
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    root.setLevel(getattr(logging, level_name, logging.INFO))
    root.propagate = False

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    redactor = RedactingFilter()

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(redactor)

    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger under the configured 'appointment_bot' root."""
    _configure_root()
    short = name.split(".")[-1] if name else "app"
    return logging.getLogger(f"appointment_bot.{short}")
