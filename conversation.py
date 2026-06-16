"""
conversation.py — Conversation state machine + booking flow (Section 7).

Responsibilities:
  - Track per-customer conversation state, keyed by WhatsApp number.
  - Drive the NEW -> AWAITING_SERVICE -> AWAITING_SLOT -> AWAITING_NAME ->
    CONFIRMED flow using FIXED message templates (Claude is used ONLY for
    intent parsing via nlu.py, never to generate customer-facing text).
  - Re-validate the chosen slot against Notion at booking time (a slot may be
    taken mid-conversation) and handle it gracefully.
  - On confirmation: write to Notion, notify admin (Telegram), optionally email.

State persistence (Tier 1): in-memory dict. **State resets if the process
restarts** — acceptable for Tier 1. Tier 2 upgrade: move this to Redis so state
survives restarts and scales across multiple workers.

Security:
  - Section 8.3 Input validation: customer name validated/sanitized here, and
    again at the Notion boundary (defence in depth).
  - Section 8.4 Rate limiting: per-number, in-memory, thread-safe; excess
    messages are ignored silently.
  - Section 8.6 PII: logs use redacted phones + booking references only.

The transport layer (app.py / whatsapp.py) calls handle_message() and sends the
returned text back to the customer. This module performs the Notion / Telegram /
email side effects itself.
"""

import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

import emailer
import notifier
import slots as slots_mod
from logger import get_logger, redact_phone
from nlu import NLUError, interpret_service, interpret_slot_choice
from notion_manager import NotionError, NotionManager

log = get_logger(__name__)

# ── Conversation states ───────────────────────────────────────────────────
STATE_NEW = "NEW"
STATE_AWAITING_SERVICE = "AWAITING_SERVICE"
STATE_AWAITING_SLOT = "AWAITING_SLOT"
STATE_AWAITING_NAME = "AWAITING_NAME"

# How many slots to offer at once.
_SLOTS_TO_SHOW = 6
# Drop a conversation that has been idle this long (seconds).
_SESSION_TTL_SECONDS = 60 * 60
# Re-prompt this many times on unclear input before escalating to a human.
_MAX_UNCLEAR_ATTEMPTS = 1

# Name validation (Section 8.3).
_MAX_NAME_LEN = 100
_MIN_NAME_LEN = 2


# ── Rate limiter (Section 8.4) ────────────────────────────────────────────
class RateLimiter:
    """Per-key sliding-window limiter: max N events per window. Thread-safe."""

    def __init__(self, max_events: int = 20, window_seconds: int = 60):
        self._max = max_events
        self._window = window_seconds
        self._events = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._events[key]
            cutoff = now - self._window
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self._max:
                return False
            q.append(now)
            return True


# ── Conversation manager ──────────────────────────────────────────────────
class ConversationManager:
    """Holds in-memory conversation state and drives the booking flow."""

    def __init__(self, config: dict = None, notion: NotionManager = None):
        self.config = config or slots_mod.load_config()
        self.notion = notion or NotionManager()
        self.tz = ZoneInfo(self.config["timezone"])
        self.business = self.config.get("business_name", "our business")

        self._sessions = {}
        self._sessions_lock = threading.Lock()
        self._per_number_locks = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()
        self._rate = RateLimiter(max_events=20, window_seconds=60)

    # ── Public entrypoint ──────────────────────────────────────────────────
    def handle_message(self, phone: str, text: str) -> str:
        """Process one inbound customer message; return the reply text to send.

        Returns None when the message is rate-limited (caller sends nothing).
        Never raises — every error path returns a safe customer-facing message
        and notifies the admin (Build Principle 4: fail safe, fail loud).
        """
        if not phone:
            return None
        text = text or ""

        if not self._rate.allow(phone):
            log.warning("Rate limit hit; ignoring message phone=%s", redact_phone(phone))
            return None

        # Serialize messages from the SAME number; allow different numbers to run
        # concurrently (so one customer's Notion call doesn't block everyone).
        with self._number_lock(phone):
            try:
                return self._dispatch(phone, text)
            except Exception as exc:  # final safety net — never crash silently
                log.error("Unhandled error phone=%s: %s", redact_phone(phone), exc)
                notifier.notify_error("conversation.handle_message", str(exc))
                return (
                    "Sorry, something went wrong on our end. A member of our "
                    "team will follow up with you shortly."
                )

    # ── State dispatch ─────────────────────────────────────────────────────
    def _dispatch(self, phone: str, text: str) -> str:
        session = self._get_session(phone)
        state = session["state"] if session else STATE_NEW

        if state == STATE_NEW:
            return self._handle_new(phone)
        if state == STATE_AWAITING_SERVICE:
            return self._handle_service(phone, session, text)
        if state == STATE_AWAITING_SLOT:
            return self._handle_slot(phone, session, text)
        if state == STATE_AWAITING_NAME:
            return self._handle_name(phone, session, text)

        # Unknown state — reset and greet.
        self._reset(phone)
        return self._handle_new(phone)

    # ── STATE: NEW ─────────────────────────────────────────────────────────
    def _handle_new(self, phone: str) -> str:
        self._set_session(phone, {"state": STATE_AWAITING_SERVICE})
        return self._service_prompt(greeting=True)

    # ── STATE: AWAITING_SERVICE ────────────────────────────────────────────
    def _handle_service(self, phone: str, session: dict, text: str) -> str:
        names = slots_mod.service_names(self.config)
        try:
            result = interpret_service(text, names)
        except NLUError:
            # Claude unavailable — fall back to numbered options (Section 8.7).
            return self._service_prompt(greeting=False)

        if result.matched:
            session["service"] = result.service
            session["unclear"] = 0
            return self._show_slots(phone, session)

        # Off-flow question, or repeatedly unclear → escalate to a human.
        if result.off_topic or session.get("unclear", 0) >= _MAX_UNCLEAR_ATTEMPTS:
            return self._escalate(phone, text, keep_state=STATE_AWAITING_SERVICE)

        session["unclear"] = session.get("unclear", 0) + 1
        self._set_session(phone, session)
        return (
            "Sorry, I didn't catch which service you'd like. Please reply with "
            "the number:\n" + self._numbered_services()
        )

    # ── STATE: AWAITING_SLOT ───────────────────────────────────────────────
    def _handle_slot(self, phone: str, session: dict, text: str) -> str:
        offered = session.get("slots", [])
        if not offered:
            # Lost the offered slots (shouldn't happen) — regenerate.
            return self._show_slots(phone, session)

        labels = [s["label"] for s in offered]
        try:
            result = interpret_slot_choice(text, labels)
        except NLUError:
            return self._slot_prompt(offered)

        if result.matched:
            chosen = offered[result.index - 1]
            session["chosen_slot"] = chosen
            session["state"] = STATE_AWAITING_NAME
            session["unclear"] = 0
            self._set_session(phone, session)
            return "Great! And what name should I put the booking under?"

        if result.off_topic or session.get("unclear", 0) >= _MAX_UNCLEAR_ATTEMPTS:
            return self._escalate(phone, text, keep_state=STATE_AWAITING_SLOT)

        session["unclear"] = session.get("unclear", 0) + 1
        self._set_session(phone, session)
        return "Please reply with the number of the time you'd like:\n" + "\n".join(
            f"{i}. {s['label']}" for i, s in enumerate(offered, 1)
        )

    # ── STATE: AWAITING_NAME ───────────────────────────────────────────────
    def _handle_name(self, phone: str, session: dict, text: str) -> str:
        name = self._validate_name(text)
        if not name:
            return (
                "Please reply with your name for the booking (up to "
                f"{_MAX_NAME_LEN} characters)."
            )

        chosen = session.get("chosen_slot")
        service = session.get("service")
        if not chosen or not service:
            # State got out of sync — restart cleanly.
            self._reset(phone)
            return self._handle_new(phone)

        # ── Re-validate the slot against Notion at booking time (Section 7) ──
        slot_start = datetime.fromisoformat(chosen["start"])
        try:
            booked = self._booked_starts()
        except NotionError:
            return self._notion_down(phone)

        if not slots_mod.is_slot_available(self.config, slot_start, booked, service):
            # Someone took it mid-conversation — apologize and re-show slots.
            log.info("Slot taken mid-conversation; re-showing slots.")
            session.pop("chosen_slot", None)
            session["state"] = STATE_AWAITING_SLOT
            self._set_session(phone, session)
            reshow = self._show_slots(phone, session)
            return (
                "Sorry — that time was just booked by someone else. "
                "Here are the latest available times:\n\n" + reshow
            )

        # ── Create the booking ──
        try:
            created = self.notion.create_booking(
                customer_name=name,
                phone=phone,
                service=service,
                appointment_dt=slot_start,
            )
        except NotionError:
            return self._notion_down(phone)

        reference = created["reference"]
        label = chosen["label"]

        # ── Notify admin (best-effort; don't block confirmation) ──
        notifier.notify_new_booking(
            business_name=self.business,
            customer_name=name,
            phone=phone,
            service=service,
            appointment_label=label,
            reference=reference,
        )

        # ── Optional email: only if enabled AND an email was collected ──
        # The Tier 1 flow does not collect an email, so this stays off unless a
        # future step populates session['email']. Wired per Section 7.
        email = session.get("email")
        if self.config.get("send_email_confirmation") and email and emailer.is_enabled():
            emailer.send_booking_confirmation(
                to_email=email,
                customer_name=name,
                business_name=self.business,
                service=service,
                appointment_label=label,
                reference=reference,
            )

        log.info(
            "Booking confirmed ref=%s service=%s phone=%s",
            reference,
            service,
            redact_phone(phone),
        )

        self._reset(phone)
        return (
            f"✅ Booking confirmed!\n\n"
            f"Service: {service}\n"
            f"When: {label}\n"
            f"Reference: {reference}\n\n"
            f"Thanks, {name}! We look forward to seeing you. "
            f"To change or cancel, just contact {self.business} directly."
        )

    # ── Slot display ───────────────────────────────────────────────────────
    def _show_slots(self, phone: str, session: dict) -> str:
        service = session.get("service")
        try:
            booked = self._booked_starts()
        except NotionError:
            return self._notion_down(phone)

        available = slots_mod.available_slots(
            self.config,
            booked_starts=booked,
            service_name=service,
            limit=_SLOTS_TO_SHOW,
        )

        if not available:
            window = self.config.get("booking_window_days")
            notifier.notify_escalation(
                self.business,
                phone,
                f"No availability for {service} in the next {window} days.",
            )
            self._reset(phone)
            return (
                f"I'm sorry — there are no available {service} times in the next "
                f"{window} days. A member of our team will reach out to help."
            )

        offered = [{"start": s.key, "label": s.label()} for s in available]
        session["slots"] = offered
        session["state"] = STATE_AWAITING_SLOT
        session["unclear"] = 0
        self._set_session(phone, session)
        return f"Great — {service}.\n\n" + self._slot_prompt(offered)

    def _slot_prompt(self, offered: list) -> str:
        lines = "\n".join(f"{i}. {s['label']}" for i, s in enumerate(offered, 1))
        return "Here are the available times:\n" + lines + "\n\nReply with the number."

    # ── Prompts / templates ────────────────────────────────────────────────
    def _service_prompt(self, greeting: bool) -> str:
        head = (
            f"Hi! Welcome to {self.business}. I can help you book an appointment.\n\n"
            if greeting
            else ""
        )
        return head + "What service would you like?\n" + self._numbered_services()

    def _numbered_services(self) -> str:
        return "\n".join(
            f"{i}. {name}"
            for i, name in enumerate(slots_mod.service_names(self.config), 1)
        )

    # ── Escalation / error responses ───────────────────────────────────────
    def _escalate(self, phone: str, text: str, keep_state: str) -> str:
        notifier.notify_escalation(self.business, phone, text)
        log.info("Escalated to admin phone=%s", redact_phone(phone))
        # Keep the conversation where it is so the customer can still continue.
        session = self._get_session(phone) or {"state": keep_state}
        session["unclear"] = 0
        self._set_session(phone, session)
        return (
            "Thanks for your message — I've flagged this for a member of our "
            "team, who will follow up with you shortly.\n\n"
            "In the meantime, if you'd like to book, just reply with the number "
            "of the option you want."
        )

    def _notion_down(self, phone: str) -> str:
        notifier.notify_error("notion", "Notion unavailable during booking flow.")
        log.error("Notion unavailable; cannot proceed phone=%s", redact_phone(phone))
        return (
            "Sorry, I'm having trouble accessing our booking system right now. "
            "A member of our team will follow up with you shortly."
        )

    # ── Notion availability lookup ─────────────────────────────────────────
    def _booked_starts(self) -> list:
        now = datetime.now(self.tz)
        window_end = now + timedelta(days=self.config["booking_window_days"] + 1)
        return self.notion.get_booked_starts(now, window_end)

    # ── Name validation (Section 8.3) ──────────────────────────────────────
    def _validate_name(self, text: str):
        if not text:
            return None
        # Reject control chars, collapse whitespace, length-limit.
        cleaned = "".join(ch for ch in text if ch == " " or ch.isprintable())
        cleaned = " ".join(cleaned.split())[:_MAX_NAME_LEN].strip()
        if len(cleaned) < _MIN_NAME_LEN:
            return None
        if not any(ch.isalpha() for ch in cleaned):
            return None  # must contain at least one letter
        return cleaned

    # ── Session storage helpers ────────────────────────────────────────────
    def _number_lock(self, phone: str) -> threading.Lock:
        with self._locks_guard:
            return self._per_number_locks[phone]

    def _get_session(self, phone: str):
        with self._sessions_lock:
            session = self._sessions.get(phone)
            if not session:
                return None
            if time.monotonic() - session.get("_ts", 0) > _SESSION_TTL_SECONDS:
                self._sessions.pop(phone, None)  # expired
                return None
            return session

    def _set_session(self, phone: str, session: dict) -> None:
        session["_ts"] = time.monotonic()
        with self._sessions_lock:
            self._sessions[phone] = session

    def _reset(self, phone: str) -> None:
        with self._sessions_lock:
            self._sessions.pop(phone, None)
