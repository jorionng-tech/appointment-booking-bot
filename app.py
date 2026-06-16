"""
app.py — Flask webhook for inbound Twilio WhatsApp messages.

Flow per inbound message (Addendum 4.2):
  1. Validate the Twilio request signature (Section 5) — reject 403 on failure.
  2. Idempotency: if MessageSid was already processed, return 200 and do nothing.
  3. Extract + sanitize: phone (digits from `From`), text (`Body`).
  4. If no Body or no From → ack 200 and do nothing.
  5. Call ConversationManager.handle_message(phone, text).
  6. If it returns a string → send via whatsapp.send_text(phone, reply).
  7. If it returns None (rate-limited) → send nothing.
  8. Return an empty 200 (TwiML) quickly so Twilio does not retry.

Replies are sent via the REST API (whatsapp.send_text), NOT via TwiML in the
response — this keeps all send logic in one place and matches the contract that
reminders.py also uses.

Security:
  - Section 5 / 6.1: Twilio signature validated on EVERY inbound POST. There is
    NO sandbox bypass — validation always runs.
  - 6.2: idempotency via MessageSid prevents double-processing on Twilio retries.
  - 6.3: Auth Token and full phone numbers never logged (phones redacted).
  - 6.5: Flask hardened — debug off in production, generic errors, no stack traces.
  - 6.4: rate limiting lives in ConversationManager; we only honour its None return.

CONFIGURATION REQUIRED (see README):
  - All REQUIRED .env vars (validated at startup) plus PUBLIC_WEBHOOK_URL, which
    must EXACTLY match the URL registered in the Twilio console (the signature is
    computed over that URL).

Production note: this uses Flask's built-in server for simplicity. For real
deployments run behind a WSGI server (gunicorn/uwsgi) + HTTPS reverse proxy.
Heavy work (Claude, Notion) runs synchronously in the request; a task queue is
the Tier 2 upgrade if throughput demands it.
"""

import base64
import hashlib
import hmac
import sys
import threading
from collections import deque

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import HTTPException

import whatsapp
from config import Config
from conversation import ConversationManager
from logger import get_logger, redact_phone

log = get_logger(__name__)

app = Flask(__name__)

# ── Lazy conversation manager (loads config + Notion client on first use) ──
_conversation = None
_conversation_lock = threading.Lock()


def get_conversation() -> ConversationManager:
    global _conversation
    if _conversation is None:
        with _conversation_lock:
            if _conversation is None:
                _conversation = ConversationManager()
    return _conversation


# ── Idempotency store (Section 6.2) ───────────────────────────────────────
class _ProcessedStore:
    """Thread-safe, bounded record of processed MessageSids."""

    def __init__(self, maxlen: int = 5000):
        self._lock = threading.Lock()
        self._order = deque()
        self._set = set()
        self._maxlen = maxlen

    def seen_or_add(self, sid: str) -> bool:
        """Return True if `sid` was already processed; otherwise record it."""
        with self._lock:
            if sid in self._set:
                return True
            self._set.add(sid)
            self._order.append(sid)
            if len(self._order) > self._maxlen:
                self._set.discard(self._order.popleft())
            return False


_processed = _ProcessedStore()


# ── Twilio signature validation (Section 5 — MANDATORY, no bypass) ─────────
def validate_twilio_signature(url: str, params: dict, signature: str) -> bool:
    """Validate the X-Twilio-Signature header. Always runs (no sandbox bypass).

    Prefers the official twilio SDK's RequestValidator; falls back to a
    hand-rolled implementation of Twilio's exact algorithm if the SDK is absent.
    """
    auth_token = Config.TWILIO_AUTH_TOKEN
    if not auth_token or not url or not signature:
        return False

    try:
        from twilio.request_validator import RequestValidator

        return RequestValidator(auth_token).validate(url, params, signature)
    except ImportError:
        return _validate_signature_fallback(auth_token, url, params, signature)


def _validate_signature_fallback(
    auth_token: str, url: str, params: dict, signature: str
) -> bool:
    """Twilio's documented algorithm: HMAC-SHA1 over url + sorted key+value."""
    data = url
    for key in sorted(params.keys()):
        data += key + params[key]
    mac = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1)
    computed = base64.b64encode(mac.digest()).decode("utf-8")
    return hmac.compare_digest(computed, signature)


# ── Helpers ────────────────────────────────────────────────────────────────
def _extract_phone(from_field: str):
    """Turn Twilio's `From` ('whatsapp:+234...') into E.164 '+234...' or None."""
    if not from_field:
        return None
    raw = from_field.strip()
    if raw.lower().startswith("whatsapp:"):
        raw = raw[len("whatsapp:"):]
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) < 7:
        return None
    return "+" + digits


def _twiml_ok() -> Response:
    """Empty TwiML 200 — acknowledges receipt; replies go via the REST API."""
    body = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
    return Response(body, status=200, mimetype="text/xml")


def _forbidden() -> Response:
    return Response("Forbidden", status=403, mimetype="text/plain")


# ── Routes ─────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="ok"), 200


@app.route("/whatsapp/webhook", methods=["POST"])
def whatsapp_webhook():
    # 1. Signature validation — reject before doing anything else.
    signature = request.headers.get("X-Twilio-Signature", "")
    params = request.form.to_dict()
    if not validate_twilio_signature(Config.PUBLIC_WEBHOOK_URL, params, signature):
        log.warning("Rejected inbound webhook: invalid Twilio signature.")
        return _forbidden()

    # 2. Idempotency — skip messages we've already handled (Twilio may retry).
    sid = request.form.get("MessageSid", "")
    if sid and _processed.seen_or_add(sid):
        log.info("Duplicate MessageSid; ignoring.")
        return _twiml_ok()

    # 3. Extract + 4. validate presence.
    phone = _extract_phone(request.form.get("From", ""))
    body = (request.form.get("Body", "") or "").strip()
    if not body or not phone:
        return _twiml_ok()

    log.info("Inbound message phone=%s", redact_phone(phone))

    # 5. Hand off to the conversation state machine (which validates/sanitizes
    #    input and runs the prompt-injection-guarded NLU internally).
    try:
        reply = get_conversation().handle_message(phone, body)
    except Exception as exc:
        # The conversation layer already fails safe; this is a final backstop so
        # Twilio always gets a fast 200 and no stack trace leaks.
        log.error("handle_message error phone=%s: %s", redact_phone(phone), exc)
        return _twiml_ok()

    # 6/7. Send a reply if there is one; None means rate-limited → send nothing.
    if reply:
        whatsapp.send_text(phone, reply)

    # 8. Fast empty 200.
    return _twiml_ok()


# ── Flask hardening (Section 6.5 / 8.9) ────────────────────────────────────
@app.errorhandler(Exception)
def _handle_exception(exc):
    """Generic error responses only — never leak stack traces to callers."""
    if isinstance(exc, HTTPException):
        return jsonify(error=exc.name), exc.code
    log.error("Unhandled application error: %s", exc)
    return jsonify(error="Internal Server Error"), 500


def main() -> int:
    """Entrypoint: validate config, then run the server."""
    from config import ensure_valid_or_exit

    ensure_valid_or_exit()

    # PUBLIC_WEBHOOK_URL is app-only (reminders.py doesn't need it), so it is
    # checked here rather than in the shared required set. Without it, signature
    # validation cannot match Twilio's computed URL.
    if not Config.PUBLIC_WEBHOOK_URL:
        log.critical(
            "FATAL: PUBLIC_WEBHOOK_URL must be set (the exact URL Twilio calls) "
            "so webhook signatures can be validated."
        )
        return 1

    # debug is OFF in production (guard on FLASK_ENV) — no interactive debugger,
    # no stack traces to callers.
    debug = not Config.is_production()
    app.run(host="0.0.0.0", port=Config.PORT, debug=debug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
