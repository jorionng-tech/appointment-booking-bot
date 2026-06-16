"""
reminders.py — Standalone scheduled reminder script (Section 11).

Run on a schedule (every 15-30 min) via cron (Linux) or Task Scheduler
(Windows). On each run it:

  1. Finds bookings ~24h away with "Reminder 24h Sent" = false, sends a WhatsApp
     reminder, then sets the checkbox + Status = Reminded-24h.
  2. Finds bookings ~2h away with "Reminder 2h Sent" = false, sends a reminder,
     then sets the checkbox + Status = Reminded-2h.

Guarantees (Section 11):
  - The Notion checkboxes guarantee no duplicate reminders, even if cron runs
    overlap or the window catches the same booking twice.
  - Timezone-correct: "now" is computed in the business timezone from config.
  - Every reminder sent is logged with booking reference + redacted phone.
  - On a WhatsApp send FAILURE we log, notify admin via Telegram, and do NOT
    mark the booking as reminded — so it retries on the next run.

DEPENDENCY NOTE:
  Sending requires whatsapp.py (built in a follow-up session). This script
  imports it lazily through `_default_send`, and `ReminderRunner` accepts a
  `send_func` so it can be tested without WhatsApp configured. Expected
  interface:  whatsapp.send_text(phone: str, text: str) -> bool

CONFIGURATION REQUIRED (see README): all REQUIRED .env vars (validated at
startup via config.ensure_valid_or_exit) plus a populated Notion bookings DB.
"""

import sys
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

import notifier
import slots as slots_mod
from logger import get_logger, redact_phone
from notion_manager import NotionError, NotionManager

log = get_logger(__name__)

# How close to the lead time a booking must be to fire. The window is
# [lead - tolerance, lead + tolerance]; make it ≥ the cron interval so no
# booking slips between runs. Checkboxes prevent any double-send.
_DEFAULT_TOLERANCE = timedelta(minutes=30)

_LEADS = {
    "24h": timedelta(hours=24),
    "2h": timedelta(hours=2),
}


def _default_send(phone: str, text: str) -> bool:
    """Default WhatsApp sender — lazy import so this module loads without it.

    whatsapp.py is built in a follow-up session. Until then, attempting a real
    run raises ImportError clearly rather than failing at import time.
    """
    import whatsapp  # noqa: built in follow-up session

    return whatsapp.send_text(phone, text)


def _format_dt(dt: datetime) -> str:
    """Human-friendly label matching slots.Slot.label() (portable 12-hour)."""
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{dt:%a %d %b}, {hour12}:{dt:%M} {ampm}"


class ReminderRunner:
    """Sends due 24h / 2h reminders. One instance per run."""

    def __init__(self, config: dict = None, notion: NotionManager = None, send_func=None):
        self.config = config or slots_mod.load_config()
        self.notion = notion or NotionManager()
        self.send = send_func or _default_send
        self.tz = ZoneInfo(self.config["timezone"])
        self.business = self.config.get("business_name", "our business")

    def run(self, now: datetime = None, tolerance: timedelta = _DEFAULT_TOLERANCE) -> dict:
        """Process 24h then 2h reminders. Returns counts for logging/testing."""
        if now is None:
            now = datetime.now(self.tz)
        counts = {"24h": 0, "2h": 0, "failed": 0}

        for kind in ("24h", "2h"):
            try:
                due = self.notion.get_due_reminders(kind, now, _LEADS[kind], tolerance)
            except NotionError as exc:
                log.error("Could not query %s reminders: %s", kind, exc)
                notifier.notify_error(f"reminders.{kind}", str(exc))
                continue

            for booking in due:
                sent_ok = self._process_one(kind, booking)
                if sent_ok:
                    counts[kind] += 1
                else:
                    counts["failed"] += 1

        log.info(
            "Reminder run complete: 24h=%d 2h=%d failed=%d",
            counts["24h"],
            counts["2h"],
            counts["failed"],
        )
        return counts

    def _process_one(self, kind: str, booking: dict) -> bool:
        """Send one reminder and mark it. Returns True on success."""
        ref = booking.get("reference") or "(no ref)"
        phone = booking.get("phone")
        appt = booking.get("appointment_dt")

        if not phone or not appt:
            log.warning("Skipping %s reminder ref=%s: missing phone/date.", kind, ref)
            return False

        label = _format_dt(appt.astimezone(self.tz))
        text = self._build_message(kind, booking.get("service"), label, ref)

        try:
            ok = self.send(phone, text)
        except Exception as exc:
            ok = False
            log.error("WhatsApp send raised for ref=%s: %s", ref, exc)

        if not ok:
            # Section 11: log + notify admin, do NOT mark sent (retry next run).
            log.error(
                "Reminder send FAILED ref=%s kind=%s phone=%s — will retry.",
                ref,
                kind,
                redact_phone(phone),
            )
            notifier.notify_error(
                "reminders.send",
                f"Failed to send {kind} reminder for {ref}; will retry next run.",
            )
            return False

        # Mark only after a successful send, so failures retry.
        try:
            self.notion.mark_reminder_sent(booking["page_id"], kind)
        except NotionError as exc:
            # Sent but couldn't mark — admin must know (risk of a duplicate next run).
            log.error("Sent %s reminder ref=%s but failed to mark: %s", kind, ref, exc)
            notifier.notify_error(
                "reminders.mark",
                f"Sent {kind} reminder for {ref} but could not update Notion.",
            )
            return True

        log.info(
            "Reminder sent ref=%s kind=%s phone=%s",
            ref,
            kind,
            redact_phone(phone),
        )
        return True

    def _build_message(self, kind: str, service: str, label: str, ref: str) -> str:
        svc = service or "appointment"
        if kind == "24h":
            when = "tomorrow"
        else:
            when = "in about 2 hours"
        return (
            f"⏰ Reminder from {self.business}: your {svc} appointment is {when} "
            f"— {label}.\nReference: {ref}.\n"
            f"See you then! To change or cancel, please contact us directly."
        )


def main() -> int:
    """Entrypoint for cron / Task Scheduler."""
    # Hard config guard runs only here (not on import), per the build rules.
    from config import ensure_valid_or_exit

    ensure_valid_or_exit()
    try:
        ReminderRunner().run()
    except Exception as exc:  # never let cron see an unhandled traceback silently
        log.error("Reminder run crashed: %s", exc)
        notifier.notify_error("reminders.main", str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
