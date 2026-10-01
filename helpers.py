"""
helpers.py — everything that isn't the conversation itself.

CSV store  · sessions + dedupe · WhatsApp send · vehicle API · audit log

The only function you'll need to rewrite for production is `call_vehicle_api`.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import os
import random
import re
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from time import monotonic, time

import httpx
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
FM_CSV = DATA_DIR / "fleet_managers.csv"
VEHICLE_CSV = DATA_DIR / "vehicles.csv"
AUDIT_CSV = DATA_DIR / "audit_log.csv"

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "").strip()
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "").strip()
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "changeme").strip()
APP_SECRET = os.getenv("APP_SECRET", "").strip()
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v21.0").strip()
ADMIN_PHONE = os.getenv("ADMIN_PHONE", "918882374849").strip()

VEHICLE_API_BASE = os.getenv("VEHICLE_API_BASE", "").strip().rstrip("/")
VEHICLE_API_KEY = os.getenv("VEHICLE_API_KEY", "").strip()
VEHICLE_API_TIMEOUT = float(os.getenv("VEHICLE_API_TIMEOUT", "10"))
# Mock mode only. 1.0 = always succeed (predictable testing).
# Set to 0.85 in .env to exercise the failure path.
MOCK_SUCCESS_RATE = float(os.getenv("MOCK_SUCCESS_RATE", "1.0"))

# No WhatsApp credentials -> print outgoing messages instead of sending them.
DRY_RUN = not (WHATSAPP_TOKEN and PHONE_NUMBER_ID)
# No vehicle API base -> simulate the vehicle responses.
MOCK_VEHICLE_API = not VEHICLE_API_BASE

ACTION_TTL = 300  # seconds a "pick an action" prompt stays valid
CONFIRM_TTL = 120  # seconds a "are you sure?" prompt stays valid
DEDUPE_TTL = 3600  # how long we remember a message id

# Flip UNLOCK/LOCK to False to make them one-tap. Keep PANIC True.
REQUIRE_CONFIRM = {"UNLOCK": True, "LOCK": True, "PANIC": True}

IST = timezone(timedelta(hours=5, minutes=30))

ACTIONS = ("UNLOCK", "LOCK", "PANIC")


# --------------------------------------------------------------------------
# CSV store
# --------------------------------------------------------------------------
_managers_by_phone: dict[str, dict] = {}
_managers_by_id: dict[str, dict] = {}
_vehicles: dict[str, dict] = {}


def normalize_phone(raw: str) -> str:
    """'+91 98123-45678' -> '919812345678'"""
    return re.sub(r"\D", "", raw or "")


def normalize_vehicle(raw: str) -> str:
    """'hrx-1234 ' -> 'HRX1234'"""
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


def looks_like_vehicle(text: str) -> bool:
    """Cheap filter so 'hi' doesn't get treated as a plate."""
    v = normalize_vehicle(text)
    if not re.fullmatch(r"[A-Z0-9]{5,13}", v):
        return False
    return any(c.isalpha() for c in v) and any(c.isdigit() for c in v)


def looks_like_greeting(text: str) -> bool:
    """Detect common greetings: hi, hello, hey, hii, namaste, etc."""
    word = text.strip().lower()
    greetings = {
        "hi", "hello", "hey", "hii", "hiiii", "hiii",
        "namaste", "salaam", "hey there", "howdy", "yo",
        "sup", "what's up", "whatsup", "good morning",
        "good afternoon", "good evening", "hola", "bonjour"
    }
    return word in greetings


def _truthy(value: str) -> bool:
    return (value or "").strip().lower() in {"yes", "y", "true", "1"}


def load_data() -> tuple[int, int]:
    """Read both CSVs into memory. Called at startup and by /reload."""
    _managers_by_phone.clear()
    _managers_by_id.clear()
    _vehicles.clear()

    with FM_CSV.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            fm = {
                "fm_id": row["fm_id"].strip(),
                "name": row["name"].strip(),
                "phone": normalize_phone(row["phone"]),
                "active": _truthy(row.get("active", "yes")),
            }
            _managers_by_phone[fm["phone"]] = fm
            _managers_by_id[fm["fm_id"]] = fm

    with VEHICLE_CSV.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            v = {
                "vehicle_no": normalize_vehicle(row["vehicle_no"]),
                "fm_id": row["fm_id"].strip(),
                "model": row.get("model", "").strip(),
                "active": _truthy(row.get("active", "yes")),
                "status": (row.get("status") or "LOCKED").strip().upper(),
            }
            _vehicles[v["vehicle_no"]] = v

    return len(_managers_by_id), len(_vehicles)


_vehicle_csv_lock = Lock()

STATUS_LABEL = {"LOCKED": "🔒 Locked", "UNLOCKED": "🔓 Unlocked"}


def get_status(vehicle_no: str) -> str:
    v = _vehicles.get(normalize_vehicle(vehicle_no))
    return v["status"] if v else "UNKNOWN"


def set_status(vehicle_no: str, status: str) -> None:
    """
    Persist the new lock state to vehicles.csv.

    Stands in for whatever the real telematics platform reports back. Rewrites
    the whole file — fine for 50 rows, replace with an UPDATE when this moves
    to a database.
    """
    vn = normalize_vehicle(vehicle_no)
    status = status.upper()

    with _vehicle_csv_lock:
        with VEHICLE_CSV.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            fields = list(reader.fieldnames or [])
            if "status" not in fields:
                fields.append("status")
            rows = []
            for row in reader:
                row.setdefault("status", "LOCKED")
                if normalize_vehicle(row["vehicle_no"]) == vn:
                    row["status"] = status
                rows.append(row)

        with VEHICLE_CSV.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    if vn in _vehicles:
        _vehicles[vn]["status"] = status


def get_manager(phone: str) -> dict | None:
    """Active fleet manager for this WhatsApp number, or None."""
    fm = _managers_by_phone.get(normalize_phone(phone))
    return fm if fm and fm["active"] else None


def get_owned_vehicle(vehicle_no: str, fm_id: str) -> dict | None:
    """
    Returns the vehicle only if it exists, is active, AND belongs to this
    manager. One return value for all three failure modes on purpose — see
    README, 'why errors are vague'.
    """
    v = _vehicles.get(normalize_vehicle(vehicle_no))
    if not v or not v["active"] or v["fm_id"] != fm_id:
        return None
    return v


def deny_reason(vehicle_no: str, fm_id: str) -> str:
    """
    Audit-only detail. The fleet manager always sees the same message; support
    still needs to tell a typo apart from a cross-fleet attempt.
    """
    v = _vehicles.get(normalize_vehicle(vehicle_no))
    if not v:
        return "DENIED_NO_SUCH_VEHICLE"
    if not v["active"]:
        return "DENIED_VEHICLE_INACTIVE"
    if v["fm_id"] != fm_id:
        return "DENIED_OTHER_FLEET"
    return "DENIED"


# --------------------------------------------------------------------------
# sessions + dedupe  (in-memory: run a SINGLE uvicorn worker)
# --------------------------------------------------------------------------
_sessions: dict[str, dict] = {}
_seen: dict[str, float] = {}
_state_lock = Lock()


def set_session(phone: str, state: str, ttl: int, **extra) -> None:
    with _state_lock:
        _sessions[phone] = {"state": state, "expires": monotonic() + ttl, **extra}


def get_session(phone: str) -> dict | None:
    """Returns the session, or None if missing/expired."""
    with _state_lock:
        s = _sessions.get(phone)
        if not s:
            return None
        if s["expires"] < monotonic():
            _sessions.pop(phone, None)
            return None
        return dict(s)


def clear_session(phone: str) -> None:
    with _state_lock:
        _sessions.pop(phone, None)


def already_seen(wamid: str) -> bool:
    """
    True if we've handled this message id before.

    Meta retries the webhook when we're slow. Without this, one sluggish
    vehicle call becomes three panic alarms.
    """
    now = time()
    with _state_lock:
        for old in [k for k, ts in _seen.items() if now - ts > DEDUPE_TTL]:
            _seen.pop(old, None)
        if wamid in _seen:
            return True
        _seen[wamid] = now
        return False


# --------------------------------------------------------------------------
# audit log — append-only, one row per stage
# --------------------------------------------------------------------------
AUDIT_FIELDS = [
    "ts_ist", "ref", "wamid", "phone", "fm_id", "vehicle_no",
    "action", "stage", "result", "http_status", "latency_ms", "error",
]
_audit_lock = Lock()


def _ensure_audit_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not AUDIT_CSV.exists():
        with AUDIT_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=AUDIT_FIELDS).writeheader()


def log_audit(
    *, stage: str, result: str, phone: str = "", fm_id: str = "",
    vehicle_no: str = "", action: str = "", wamid: str = "", ref: str = "",
    http_status: str = "", latency_ms: str = "", error: str = "",
) -> None:
    _ensure_audit_file()
    row = {
        "ts_ist": datetime.now(IST).isoformat(timespec="seconds"),
        "ref": ref, "wamid": wamid, "phone": phone, "fm_id": fm_id,
        "vehicle_no": vehicle_no, "action": action, "stage": stage,
        "result": result, "http_status": http_status,
        "latency_ms": latency_ms, "error": error,
    }
    with _audit_lock:
        with AUDIT_CSV.open("a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=AUDIT_FIELDS).writerow(row)
    print(f"[audit] {stage:<16} {result:<22} {phone or '-':<14} "
          f"{vehicle_no or '-':<10} {action or '-'}")


def read_audit(limit: int = 50) -> list[dict]:
    if not AUDIT_CSV.exists():
        return []
    with AUDIT_CSV.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))[-limit:]


def new_ref() -> str:
    return "REQ-" + uuid.uuid4().hex[:6].upper()


def now_ist() -> str:
    """
    '30 Sep 2026, 4:12 PM IST'

    Built by hand rather than with strftime's %-d / %-I (strip leading zero),
    which are a glibc extension and raise ValueError on Windows.
    """
    t = datetime.now(IST)
    return (f"{t.day} {t.strftime('%b %Y')}, "
            f"{t.hour % 12 or 12}:{t.strftime('%M %p')} IST")


# --------------------------------------------------------------------------
# WhatsApp Cloud API
# --------------------------------------------------------------------------
OUTBOX: deque = deque(maxlen=200)  # every outgoing message, for /simulate + debugging


async def _post_to_meta(payload: dict) -> None:
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload, headers=headers)
        if r.status_code >= 400:
            print(f"[whatsapp] send failed {r.status_code}: {r.text[:300]}")
    except Exception as exc:  # never let a send failure kill the handler
        print(f"[whatsapp] send error: {exc}")


async def send_text(to: str, body: str) -> None:
    OUTBOX.append({"to": to, "kind": "text", "body": body})
    if DRY_RUN:
        print(f"\n--- to {to} ---\n{body}\n")
        return
    await _post_to_meta({
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    })


async def send_buttons(to: str, body: str, buttons: list[tuple[str, str]]) -> None:
    """buttons: [(payload_id, title)] — max 3, titles truncated to 20 chars."""
    buttons = buttons[:3]
    OUTBOX.append({"to": to, "kind": "buttons", "body": body, "buttons": buttons})
    if DRY_RUN:
        rendered = "  |  ".join(f"[{t}]" for _, t in buttons)
        print(f"\n--- to {to} ---\n{body}\n{rendered}\n")
        return
    await _post_to_meta({
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body[:1024]},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": bid[:256], "title": title[:20]}}
                    for bid, title in buttons
                ]
            },
        },
    })


def verify_signature(raw_body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256 check. Skipped when APP_SECRET is unset."""
    if not APP_SECRET:
        return True
    if not header:
        print("[sig] REJECTED: no X-Hub-Signature-256 header. Meta strips it on "
              "a redirect — check for a trailing slash in your Callback URL.")
        return False
    if not header.startswith("sha256="):
        print(f"[sig] REJECTED: malformed header {header[:20]!r}")
        return False
    expected = hmac.new(APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header.split("=", 1)[1]):
        print("[sig] REJECTED: signature mismatch. APP_SECRET in .env probably "
              "doesn't match App Settings → Basic → App Secret. "
              "Blank APP_SECRET to disable this check while testing.")
        return False
    return True


def parse_incoming(payload: dict) -> list[dict]:
    """
    Flatten a Meta webhook into [{wamid, phone, type, text, button_id}].
    Delivery/read 'statuses' events are dropped.
    """
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []) or []:
                item = {
                    "wamid": msg.get("id", ""),
                    "phone": msg.get("from", ""),
                    "type": msg.get("type", ""),
                    "text": "",
                    "button_id": "",
                }
                if item["type"] == "text":
                    item["text"] = msg.get("text", {}).get("body", "")
                elif item["type"] == "interactive":
                    inter = msg.get("interactive", {})
                    reply = inter.get("button_reply") or inter.get("list_reply") or {}
                    item["button_id"] = reply.get("id", "")
                elif item["type"] == "button":  # template quick-reply
                    item["button_id"] = msg.get("button", {}).get("payload", "")
                out.append(item)
    return out


# --------------------------------------------------------------------------
# vehicle control API  <- replace this for production
# --------------------------------------------------------------------------
async def call_vehicle_api(action: str, vehicle_no: str) -> dict:
    """
    Returns {ok, status, latency_ms, error}.

    Mock mode (no VEHICLE_API_BASE) succeeds ~85% of the time so you can see
    both the success and the failure message without a real device.
    """
    started = monotonic()

    if MOCK_VEHICLE_API:
        await asyncio.sleep(random.uniform(0.6, 1.8))
        ok = random.random() < MOCK_SUCCESS_RATE
        if ok and action in ("UNLOCK", "LOCK"):
            # Persist the new state so the CSV reflects reality between
            # messages. PANIC doesn't change the lock state.
            set_status(vehicle_no, "UNLOCKED" if action == "UNLOCK" else "LOCKED")
        return {
            "ok": ok,
            "status": 200 if ok else 504,
            "latency_ms": int((monotonic() - started) * 1000),
            "error": "" if ok else "DEVICE_UNREACHABLE",
        }

    endpoint = {
        "UNLOCK": "/vehicle/unlock",
        "LOCK": "/vehicle/lock",
        "PANIC": "/vehicle/panic",
    }[action]
    headers = {"Authorization": f"Bearer {VEHICLE_API_KEY}"} if VEHICLE_API_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=VEHICLE_API_TIMEOUT) as client:
            r = await client.post(
                VEHICLE_API_BASE + endpoint,
                json={"vehicle_no": vehicle_no},
                headers=headers,
            )
        return {
            "ok": 200 <= r.status_code < 300,
            "status": r.status_code,
            "latency_ms": int((monotonic() - started) * 1000),
            "error": "" if r.status_code < 300 else r.text[:120],
        }
    except httpx.TimeoutException:
        return {"ok": False, "status": 0,
                "latency_ms": int((monotonic() - started) * 1000),
                "error": "TIMEOUT"}
    except Exception as exc:
        return {"ok": False, "status": 0,
                "latency_ms": int((monotonic() - started) * 1000),
                "error": str(exc)[:120]}


# Load at import, not just on startup — otherwise anything that reaches the
# handlers before the startup hook fires (tests, embedded use) sees an empty
# store and denies every real fleet manager.
load_data()