"""
notion_manager.py — Bookings database read/write (Notion).

Responsibilities:
  - Create a booking page with all Section 5 properties.
  - Read existing bookings to compute taken slots (for slots.py).
  - Query bookings due for 24h / 2h reminders (for reminders.py).
  - Update booking status + reminder checkboxes.
  - Generate unique short booking references (BK-XXXXX).

Security:
  - Section 8.3 Input Validation: customer name / notes are sanitized
    (control chars stripped, length-limited) before they ever reach Notion —
    defence-in-depth at the database boundary.
  - Section 8.8 Notion Injection Safety: query filters use STRUCTURED fields
    only (status select, date ranges built from datetimes). Raw customer text
    is never interpolated into query logic.
  - Section 8.6 PII: logs use booking references and redacted phones only.

CONFIGURATION REQUIRED before this module can talk to Notion (see README):
  - NOTION_TOKEN ............ Notion integration token (.env)
  - NOTION_BOOKINGS_DB_ID ... the Appointments database ID (.env)
  - The database must have the EXACT properties + types from SKILL.md Section 5,
    and must be shared with the integration.
  Until those are set, importing this module is safe; the Notion client is
  created lazily on first real call.
"""

import re
import secrets
import string
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from config import Config
from logger import get_logger, redact_phone

log = get_logger(__name__)

# ── Exact Notion property names (Section 5) — single source of truth ──
PROP_NAME = "Customer Name"
PROP_PHONE = "Phone / WhatsApp"
PROP_SERVICE = "Service"
PROP_DATE = "Appointment Date"
PROP_STATUS = "Status"
PROP_REF = "Booking Reference"
PROP_CREATED = "Created At"
PROP_REMINDER_24H = "Reminder 24h Sent"
PROP_REMINDER_2H = "Reminder 2h Sent"
PROP_NOTES = "Notes"

# ── Status select options (Section 5) ──
STATUS_BOOKED = "Booked"
STATUS_REMINDED_24H = "Reminded-24h"
STATUS_REMINDED_2H = "Reminded-2h"
STATUS_COMPLETED = "Completed"
STATUS_CANCELLED = "Cancelled"
STATUS_NO_SHOW = "No-Show"

# Statuses that still occupy a slot (used when computing availability).
_ACTIVE_STATUSES = {
    STATUS_BOOKED,
    STATUS_REMINDED_24H,
    STATUS_REMINDED_2H,
    STATUS_COMPLETED,
}

_MAX_NAME_LEN = 100
_MAX_NOTES_LEN = 1000
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class NotionError(RuntimeError):
    """Raised on any Notion API failure so callers can notify admin + fail safe."""


# ── Sanitization (Section 8.3) ────────────────────────────────────────────
def sanitize_text(value: str, max_len: int) -> str:
    """Strip control characters, collapse whitespace, and length-limit."""
    if value is None:
        return ""
    text = _CONTROL_CHARS_RE.sub("", str(value))
    text = " ".join(text.split())  # collapse runs of whitespace
    return text[:max_len].strip()


def sanitize_name(name: str) -> str:
    return sanitize_text(name, _MAX_NAME_LEN)


def sanitize_notes(notes: str) -> str:
    return sanitize_text(notes, _MAX_NOTES_LEN)


# ── Booking reference ─────────────────────────────────────────────────────
_REF_ALPHABET = string.ascii_uppercase + string.digits


def generate_booking_reference() -> str:
    """Generate a unique short code like 'BK-7F3A2' (cryptographically random)."""
    suffix = "".join(secrets.choice(_REF_ALPHABET) for _ in range(5))
    return f"BK-{suffix}"


# ── Notion manager ────────────────────────────────────────────────────────
class NotionManager:
    """Thin wrapper over the Notion SDK for the bookings database."""

    def __init__(self, token: str = None, db_id: str = None):
        self._token = token or Config.NOTION_TOKEN
        self._db_id = db_id or Config.NOTION_BOOKINGS_DB_ID
        self._client = None  # lazy

    @property
    def client(self):
        """Lazily construct the Notion client so import never requires creds."""
        if self._client is None:
            if not self._token or not self._db_id:
                raise NotionError(
                    "Notion not configured: set NOTION_TOKEN and "
                    "NOTION_BOOKINGS_DB_ID in .env."
                )
            try:
                from notion_client import Client
            except ImportError as exc:  # pragma: no cover
                raise NotionError(
                    "notion-client not installed; run pip install -r requirements.txt"
                ) from exc
            self._client = Client(auth=self._token)
        return self._client

    # ── Create ────────────────────────────────────────────────────────────
    def create_booking(
        self,
        customer_name: str,
        phone: str,
        service: str,
        appointment_dt: datetime,
        notes: str = "",
        booking_reference: str = None,
    ) -> dict:
        """Create a Booked appointment. Returns {'page_id', 'reference'}.

        All free-text customer input is sanitized here before reaching Notion.
        `appointment_dt` must be a timezone-aware datetime.
        """
        ref = booking_reference or generate_booking_reference()
        clean_name = sanitize_name(customer_name)
        clean_notes = sanitize_notes(notes)
        # "Created At" in the same (business) timezone as the appointment.
        created = datetime.now(appointment_dt.tzinfo or ZoneInfo("UTC"))

        properties = {
            PROP_NAME: {"title": [{"text": {"content": clean_name or "Unknown"}}]},
            PROP_PHONE: {"phone_number": phone},
            PROP_SERVICE: {"select": {"name": service}},
            PROP_DATE: {"date": {"start": appointment_dt.isoformat()}},
            PROP_STATUS: {"select": {"name": STATUS_BOOKED}},
            PROP_REF: {"rich_text": [{"text": {"content": ref}}]},
            PROP_CREATED: {"date": {"start": created.isoformat()}},
            PROP_REMINDER_24H: {"checkbox": False},
            PROP_REMINDER_2H: {"checkbox": False},
            PROP_NOTES: {"rich_text": [{"text": {"content": clean_notes}}]},
        }

        try:
            page = self.client.pages.create(
                parent={"database_id": self._db_id}, properties=properties
            )
        except Exception as exc:
            log.error("Notion create_booking failed ref=%s: %s", ref, exc)
            raise NotionError(f"Failed to create booking {ref}") from exc

        log.info(
            "Booking created ref=%s service=%s phone=%s",
            ref,
            service,
            redact_phone(phone),
        )
        return {"page_id": page["id"], "reference": ref}

    # ── Read: availability ──────────────────────────────────────────────────
    def get_booked_starts(self, window_start: datetime, window_end: datetime) -> list:
        """Return start datetimes of active bookings in [window_start, window_end].

        Used by slots.py to remove already-taken times. Filter is built from
        structured fields only (date range + status), never from user text.
        """
        date_filter = {
            "and": [
                {
                    "property": PROP_DATE,
                    "date": {"on_or_after": window_start.isoformat()},
                },
                {
                    "property": PROP_DATE,
                    "date": {"on_or_before": window_end.isoformat()},
                },
            ]
        }
        starts = []
        for page in self._query_all(date_filter):
            props = page.get("properties", {})
            status = self._read_select(props, PROP_STATUS)
            if status not in _ACTIVE_STATUSES:
                continue
            start = self._read_date(props, PROP_DATE)
            if start:
                starts.append(start)
        return starts

    # ── Read: reminders ─────────────────────────────────────────────────────
    def get_due_reminders(
        self, kind: str, now: datetime, lead: timedelta, tolerance: timedelta
    ) -> list:
        """Return bookings whose appointment is ~`lead` away and not yet reminded.

        `kind` is '24h' or '2h'. A booking is due when its appointment time falls
        in [now+lead-tolerance, now+lead+tolerance] and the matching reminder
        checkbox is still false. Filters use structured fields only.
        """
        if kind == "24h":
            checkbox_prop = PROP_REMINDER_24H
            allowed_status = {STATUS_BOOKED}
        elif kind == "2h":
            checkbox_prop = PROP_REMINDER_2H
            allowed_status = {STATUS_BOOKED, STATUS_REMINDED_24H}
        else:
            raise ValueError("kind must be '24h' or '2h'")

        target = now + lead
        win_start = target - tolerance
        win_end = target + tolerance

        notion_filter = {
            "and": [
                {"property": checkbox_prop, "checkbox": {"equals": False}},
                {"property": PROP_DATE, "date": {"on_or_after": win_start.isoformat()}},
                {"property": PROP_DATE, "date": {"on_or_before": win_end.isoformat()}},
            ]
        }

        due = []
        for page in self._query_all(notion_filter):
            props = page.get("properties", {})
            if self._read_select(props, PROP_STATUS) not in allowed_status:
                continue
            due.append(
                {
                    "page_id": page["id"],
                    "reference": self._read_rich_text(props, PROP_REF),
                    "name": self._read_title(props, PROP_NAME),
                    "phone": self._read_phone(props, PROP_PHONE),
                    "service": self._read_select(props, PROP_SERVICE),
                    "appointment_dt": self._read_date(props, PROP_DATE),
                }
            )
        return due

    # ── Update ──────────────────────────────────────────────────────────────
    def update_status(self, page_id: str, status: str) -> None:
        self._update_properties(page_id, {PROP_STATUS: {"select": {"name": status}}})

    def mark_reminder_sent(self, page_id: str, kind: str) -> None:
        """Set the reminder checkbox + advance status. `kind` is '24h' or '2h'."""
        if kind == "24h":
            props = {
                PROP_REMINDER_24H: {"checkbox": True},
                PROP_STATUS: {"select": {"name": STATUS_REMINDED_24H}},
            }
        elif kind == "2h":
            props = {
                PROP_REMINDER_2H: {"checkbox": True},
                PROP_STATUS: {"select": {"name": STATUS_REMINDED_2H}},
            }
        else:
            raise ValueError("kind must be '24h' or '2h'")
        self._update_properties(page_id, props)

    def _update_properties(self, page_id: str, properties: dict) -> None:
        try:
            self.client.pages.update(page_id=page_id, properties=properties)
        except Exception as exc:
            log.error("Notion update failed page=%s: %s", page_id, exc)
            raise NotionError("Failed to update booking") from exc

    # ── Internal query helper (handles pagination) ──────────────────────────
    def _query_all(self, notion_filter: dict) -> list:
        results = []
        cursor = None
        try:
            while True:
                kwargs = {"database_id": self._db_id, "filter": notion_filter}
                if cursor:
                    kwargs["start_cursor"] = cursor
                resp = self.client.databases.query(**kwargs)
                results.extend(resp.get("results", []))
                if not resp.get("has_more"):
                    break
                cursor = resp.get("next_cursor")
        except Exception as exc:
            log.error("Notion query failed: %s", exc)
            raise NotionError("Failed to query bookings") from exc
        return results

    # ── Property readers (defensive against missing/empty values) ───────────
    @staticmethod
    def _read_select(props: dict, name: str):
        sel = (props.get(name) or {}).get("select")
        return sel.get("name") if sel else None

    @staticmethod
    def _read_date(props: dict, name: str):
        date = (props.get(name) or {}).get("date")
        if not date or not date.get("start"):
            return None
        try:
            return datetime.fromisoformat(date["start"])
        except ValueError:
            return None

    @staticmethod
    def _read_phone(props: dict, name: str):
        return (props.get(name) or {}).get("phone_number")

    @staticmethod
    def _read_title(props: dict, name: str):
        title = (props.get(name) or {}).get("title") or []
        return "".join(t.get("plain_text", "") for t in title)

    @staticmethod
    def _read_rich_text(props: dict, name: str):
        rt = (props.get(name) or {}).get("rich_text") or []
        return "".join(t.get("plain_text", "") for t in rt)
