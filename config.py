"""
config.py — Loads and validates environment configuration at startup.

Design (per build instruction):
  - Importing this module NEVER exits the process, even if .env is missing.
    Validation results are exposed via `validate_config()` and `Config`.
  - The hard "fail loud and exit" guard runs ONLY when the app explicitly
    calls `ensure_valid_or_exit()` at boot (app.py / reminders.py entrypoints).

Security (Section 8.5):
  - Secrets come from environment variables only — never hardcoded here.
  - This module never logs secret values.

REQUIRED groups: Twilio (WhatsApp), Notion, Claude, Telegram.
OPTIONAL: Brevo (the bot degrades gracefully without it).

Provider note: this build uses Twilio WhatsApp (not Meta Cloud API). The send
API, webhook payload, and signature method differ; everything else is identical.
"""

import os
import sys

try:
    from dotenv import load_dotenv

    # Load .env if present. Absent .env is fine — values may come from the real
    # environment (e.g. container/secret manager). No error on missing file.
    load_dotenv()
except ImportError:
    # python-dotenv not installed yet — env vars can still be read from os.environ.
    pass


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class Config:
    """Typed accessors for all configuration values."""

    # ── Twilio WhatsApp (REQUIRED) ──
    TWILIO_ACCOUNT_SID = _get("TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN = _get("TWILIO_AUTH_TOKEN")
    TWILIO_WHATSAPP_FROM = _get("TWILIO_WHATSAPP_FROM")

    # Public URL Twilio calls for the webhook — must match the Twilio console
    # EXACTLY (signature validation depends on it). Required by app.py, which
    # checks it at startup; reminders.py does not need it.
    PUBLIC_WEBHOOK_URL = _get("PUBLIC_WEBHOOK_URL")

    # ── Notion (REQUIRED) ──
    NOTION_TOKEN = _get("NOTION_TOKEN")
    NOTION_BOOKINGS_DB_ID = _get("NOTION_BOOKINGS_DB_ID")

    # ── Claude / Anthropic (REQUIRED) ──
    ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")

    # ── Telegram (REQUIRED) ──
    TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN")
    TELEGRAM_ADMIN_CHAT_ID = _get("TELEGRAM_ADMIN_CHAT_ID")

    # ── Brevo (OPTIONAL) ──
    BREVO_API_KEY = _get("BREVO_API_KEY")
    BREVO_SENDER_EMAIL = _get("BREVO_SENDER_EMAIL")
    BREVO_SENDER_NAME = _get("BREVO_SENDER_NAME", "Appointment Bot")

    # ── App config ──
    FLASK_ENV = _get("FLASK_ENV", "production")
    PORT = int(_get("PORT", "5001") or "5001")

    # Claude model used for intent parsing. Defaults to the latest Opus.
    # High-volume deployments can set ANTHROPIC_MODEL=claude-haiku-4-5 in .env
    # to trade some accuracy for lower cost/latency on this classification task.
    ANTHROPIC_MODEL = _get("ANTHROPIC_MODEL", "claude-opus-4-8")

    @classmethod
    def is_production(cls) -> bool:
        return cls.FLASK_ENV.lower() == "production"

    @classmethod
    def brevo_enabled(cls) -> bool:
        """Brevo is usable only if all three of its values are present."""
        return bool(
            cls.BREVO_API_KEY and cls.BREVO_SENDER_EMAIL and cls.BREVO_SENDER_NAME
        )


# Required vars grouped by integration — used by validation.
REQUIRED_VARS = {
    "Twilio": [
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_WHATSAPP_FROM",
    ],
    "Notion": [
        "NOTION_TOKEN",
        "NOTION_BOOKINGS_DB_ID",
    ],
    "Claude": [
        "ANTHROPIC_API_KEY",
    ],
    "Telegram": [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ADMIN_CHAT_ID",
    ],
}


def validate_config() -> list:
    """Return a list of missing REQUIRED env var names. Empty list == valid.

    Pure check — does not log secrets, does not exit. Safe to call from tests.
    """
    missing = []
    for _group, names in REQUIRED_VARS.items():
        for name in names:
            if not _get(name):
                missing.append(name)
    return missing


def format_errors() -> list:
    """Return a list of format problems in otherwise-present config values.

    These are fatal (a malformed value would silently misbehave), but distinct
    from 'missing'. Pure check — no logging, no exit.
    """
    errors = []
    from_value = _get("TWILIO_WHATSAPP_FROM")
    if from_value and not from_value.startswith("whatsapp:+"):
        errors.append(
            "TWILIO_WHATSAPP_FROM must start with 'whatsapp:+' "
            "(e.g. whatsapp:+14155238886)."
        )
    return errors


def config_warnings() -> list:
    """Non-fatal configuration warnings (e.g. insecure settings)."""
    warnings = []
    if Config.FLASK_ENV.lower() == "development":
        warnings.append(
            "FLASK_ENV=development — debug features must NEVER run in production."
        )
    if not Config.brevo_enabled():
        warnings.append(
            "Brevo not fully configured — email confirmations are disabled "
            "(optional; the bot works without it)."
        )
    return warnings


def ensure_valid_or_exit() -> None:
    """Hard startup guard. Call ONLY from real entrypoints (app/reminders).

    Importing config.py does not trigger this. If required vars are missing,
    log a clear fatal error (no secret values) and exit non-zero — the spec's
    "do not start a half-working bot" rule (Section 4 / Build Principle 4).
    """
    # Imported lazily so importing config.py never pulls in logging side effects.
    from logger import get_logger

    log = get_logger(__name__)

    missing = validate_config()
    if missing:
        log.critical(
            "FATAL: missing required configuration: %s. "
            "Copy .env.example to .env and fill in the required values.",
            ", ".join(missing),
        )
        sys.exit(1)

    errors = format_errors()
    if errors:
        for err in errors:
            log.critical("FATAL: invalid configuration: %s", err)
        sys.exit(1)

    for warning in config_warnings():
        log.warning("Config warning: %s", warning)

    log.info("Configuration validated: all required credentials present.")
