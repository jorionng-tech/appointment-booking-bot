"""
slots.py — Availability slot generation from booking_config.json.

Responsibilities:
  - Load + validate booking_config.json.
  - Generate candidate appointment slots for the next `booking_window_days`,
    honouring working hours, break times, slot grid, and service duration.
  - Remove already-taken slots given existing bookings (respecting
    `max_bookings_per_slot`).

Design note: this module is deliberately API-free so it is unit-testable
without Notion or any network. The caller (conversation.py) fetches existing
bookings from Notion and passes their start times into `available_slots()`.

Timezone: all slots are timezone-aware in the business timezone from config.
"""

import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 fallback
    from backports.zoneinfo import ZoneInfo  # type: ignore

from logger import get_logger

log = get_logger(__name__)

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "booking_config.json"
)

_WEEKDAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


class ConfigError(ValueError):
    """Raised when booking_config.json is missing or structurally invalid."""


# ── Slot model ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Slot:
    """A bookable time slot, timezone-aware in the business timezone."""

    start: datetime
    end: datetime

    @property
    def key(self) -> str:
        """Canonical identity used to match against existing bookings."""
        return self.start.isoformat()

    def label(self) -> str:
        """Human-friendly label, e.g. 'Mon 16 Jun, 10:00 AM'."""
        # %-I is non-portable (fails on Windows); format the hour manually.
        hour12 = self.start.hour % 12 or 12
        ampm = "AM" if self.start.hour < 12 else "PM"
        return (
            f"{self.start:%a %d %b}, "
            f"{hour12}:{self.start:%M} {ampm}"
        )


# ── Config loading + validation ───────────────────────────────────────────
def _parse_hm(value: str, field: str) -> time:
    """Parse 'HH:MM' into a time, raising ConfigError on bad input."""
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        raise ConfigError(f"Invalid time '{value}' in {field}; expected 'HH:MM'.")


def load_config(path: str = CONFIG_PATH) -> dict:
    """Load and validate booking_config.json. Raises ConfigError on problems."""
    if not os.path.exists(path):
        raise ConfigError(f"booking_config.json not found at {path}")

    try:
        with open(path, "r", encoding="utf-8") as fh:
            config = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"booking_config.json is not valid JSON: {exc}")

    _validate_config(config)
    return config


def _validate_config(config: dict) -> None:
    required = [
        "business_name",
        "timezone",
        "booking_window_days",
        "slot_duration_minutes",
        "working_hours",
        "services",
    ]
    for key in required:
        if key not in config:
            raise ConfigError(f"booking_config.json missing required key: '{key}'")

    # Timezone must resolve.
    try:
        ZoneInfo(config["timezone"])
    except Exception:
        raise ConfigError(f"Unknown timezone: '{config['timezone']}'")

    if not isinstance(config["booking_window_days"], int) or config[
        "booking_window_days"
    ] < 1:
        raise ConfigError("booking_window_days must be a positive integer.")

    if not isinstance(config["slot_duration_minutes"], int) or config[
        "slot_duration_minutes"
    ] < 1:
        raise ConfigError("slot_duration_minutes must be a positive integer.")

    # Working hours: each weekday must be present and either null or open/close.
    for day in _WEEKDAYS:
        if day not in config["working_hours"]:
            raise ConfigError(f"working_hours missing day: '{day}'")
        hours = config["working_hours"][day]
        if hours is None:
            continue
        if "open" not in hours or "close" not in hours:
            raise ConfigError(f"working_hours['{day}'] needs 'open' and 'close'.")
        open_t = _parse_hm(hours["open"], f"working_hours['{day}'].open")
        close_t = _parse_hm(hours["close"], f"working_hours['{day}'].close")
        if open_t >= close_t:
            raise ConfigError(f"working_hours['{day}'] open must be before close.")

    # Break times (optional).
    for brk in config.get("break_times", []) or []:
        _parse_hm(brk["start"], "break_times.start")
        _parse_hm(brk["end"], "break_times.end")

    # Services.
    if not config["services"]:
        raise ConfigError("At least one service must be defined.")
    for svc in config["services"]:
        if "name" not in svc:
            raise ConfigError("Each service needs a 'name'.")

    config.setdefault("max_bookings_per_slot", 1)
    config.setdefault("break_times", [])
    config.setdefault("send_email_confirmation", False)


# ── Service helpers ───────────────────────────────────────────────────────
def get_service(config: dict, name: str):
    """Return the service dict matching `name` (case-insensitive), or None."""
    if not name:
        return None
    target = name.strip().lower()
    for svc in config["services"]:
        if svc["name"].strip().lower() == target:
            return svc
    return None


def service_names(config: dict) -> list:
    return [svc["name"] for svc in config["services"]]


# ── Slot generation ───────────────────────────────────────────────────────
def _overlaps_break(start: datetime, end: datetime, breaks, tzinfo) -> bool:
    """True if [start, end) overlaps any configured break on start's date."""
    day = start.date()
    for brk in breaks:
        b_start = datetime.combine(day, _parse_hm(brk["start"], "break"), tzinfo)
        b_end = datetime.combine(day, _parse_hm(brk["end"], "break"), tzinfo)
        # Half-open overlap test.
        if start < b_end and end > b_start:
            return True
    return False


def generate_candidate_slots(
    config: dict,
    service_duration_minutes: int = None,
    now: datetime = None,
) -> list:
    """Generate all candidate slots in the booking window (ignores bookings).

    `service_duration_minutes` defaults to the slot grid duration. A slot is
    only emitted if the full service fits before closing and clears all breaks.
    Past slots (start <= now) are excluded.
    """
    tz = ZoneInfo(config["timezone"])
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    grid = config["slot_duration_minutes"]
    service_dur = service_duration_minutes or grid
    breaks = config.get("break_times", []) or []
    window = config["booking_window_days"]

    slots = []
    for offset in range(window):
        date = (now + timedelta(days=offset)).date()
        weekday = _WEEKDAYS[date.weekday()]
        hours = config["working_hours"].get(weekday)
        if not hours:
            continue  # closed that day

        open_dt = datetime.combine(date, _parse_hm(hours["open"], "open"), tz)
        close_dt = datetime.combine(date, _parse_hm(hours["close"], "close"), tz)

        cursor = open_dt
        step = timedelta(minutes=grid)
        service_delta = timedelta(minutes=service_dur)
        while cursor + service_delta <= close_dt:
            slot_end = cursor + service_delta
            if cursor > now and not _overlaps_break(cursor, slot_end, breaks, tz):
                slots.append(Slot(start=cursor, end=slot_end))
            cursor += step

    return slots


def available_slots(
    config: dict,
    booked_starts=None,
    service_name: str = None,
    now: datetime = None,
    limit: int = None,
) -> list:
    """Return bookable slots with already-taken ones removed.

    Args:
        config: validated config dict.
        booked_starts: iterable of existing booking start datetimes (tz-aware
            or ISO strings) fetched from Notion. Counted per-slot.
        service_name: if given, slots are sized to that service's duration.
        now: reference time (defaults to current time in business tz).
        limit: cap on number of slots returned (e.g. show top 5).

    A slot is removed once its booking count reaches `max_bookings_per_slot`.
    """
    service_dur = None
    if service_name:
        svc = get_service(config, service_name)
        if svc:
            service_dur = svc.get("duration_minutes")

    candidates = generate_candidate_slots(config, service_dur, now)

    taken = _count_booked(config, booked_starts)
    max_per = config.get("max_bookings_per_slot", 1)

    result = [s for s in candidates if taken.get(s.key, 0) < max_per]

    if limit is not None:
        result = result[:limit]
    return result


def _count_booked(config: dict, booked_starts) -> Counter:
    """Normalise booked start times into a Counter keyed by ISO start string."""
    counter: Counter = Counter()
    if not booked_starts:
        return counter

    tz = ZoneInfo(config["timezone"])
    for raw in booked_starts:
        dt = _coerce_dt(raw, tz)
        if dt is not None:
            counter[dt.isoformat()] += 1
    return counter


def _coerce_dt(raw, tz):
    """Coerce an ISO string or datetime into a tz-aware datetime in `tz`."""
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            log.warning("Skipping unparseable booking start time.")
            return None
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def is_slot_available(
    config: dict,
    slot_start: datetime,
    booked_starts,
    service_name: str = None,
    now: datetime = None,
) -> bool:
    """Re-validate a single slot at booking time (Section 7 / 13 requirement).

    Used right before writing to Notion to catch a slot taken mid-conversation.
    `now` is injectable for testing; defaults to current time in business tz.
    """
    tz = ZoneInfo(config["timezone"])
    target = _coerce_dt(slot_start, tz)
    if target is None:
        return False

    svc = get_service(config, service_name) if service_name else None
    service_dur = svc.get("duration_minutes") if svc else None

    # Must still be a generated, in-window, future slot.
    candidates = {
        s.key for s in generate_candidate_slots(config, service_dur, now)
    }
    if target.isoformat() not in candidates:
        return False

    taken = _count_booked(config, booked_starts)
    return taken.get(target.isoformat(), 0) < config.get("max_bookings_per_slot", 1)
