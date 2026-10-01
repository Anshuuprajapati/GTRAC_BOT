"""
helpers.py — everything that isn't the conversation itself.

CSV store · ControlRoomAdmins roster + shifts · EngineeringTeam ·
pending approvals · sessions + dedupe · portal OTP auth · WhatsApp send ·
vehicle API · audit log

Who's who: FleetManagers request actions, ControlRoomAdmins (shift staff)
approve them, EngineeringTeam_<ROLE> run the roster portal and are the
fallback approvers.

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
import secrets
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
DATA_DIR = Path(os.getenv("DATA_DIR") or BASE_DIR / "data")
FM_CSV = DATA_DIR / "fleet_managers.csv"
VEHICLE_CSV = DATA_DIR / "vehicles.csv"
CONTROL_ROOM_CSV = DATA_DIR / "control_room_admins.csv"
AUDIT_CSV = DATA_DIR / "audit_log.csv"

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "").strip()
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "").strip()
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "changeme").strip()
APP_SECRET = os.getenv("APP_SECRET", "").strip()
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v21.0").strip()
TIMEZONE_STR = os.getenv("TIMEZONE", "Asia/Kolkata").strip()

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
# /simulate skips the Meta signature check, so anyone who can reach it can
# act as any fleet manager or approver. Off by default once real credentials
# are in place; flip on deliberately for local testing.
ENABLE_SIMULATE = os.getenv("ENABLE_SIMULATE", "yes" if DRY_RUN else "no").strip().lower() in {
    "yes", "y", "true", "1"}

ACTION_TTL = 300    # seconds a "pick an action" prompt stays valid
CONFIRM_TTL = 120   # seconds a "are you sure?" prompt stays valid
APPROVAL_TTL = int(os.getenv("APPROVAL_TTL", "600"))  # seconds approvers have to decide
DEDUPE_TTL = 3600   # how long we remember a message id

# Flip UNLOCK/LOCK to False to skip the manager's own "are you sure?" step.
# ControlRoomAdmin approval still applies either way.
REQUIRE_CONFIRM = {"UNLOCK": True, "LOCK": True, "PANIC": True}

IST = timezone(timedelta(hours=5, minutes=30))

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(TIMEZONE_STR)
except Exception:
    # On Windows zoneinfo needs the `tzdata` package; without it every name
    # fails to resolve and shifts would silently run on IST.
    print(f"[config] WARNING: timezone {TIMEZONE_STR!r} not found "
          "(pip install tzdata). Falling back to IST for shifts.")
    LOCAL_TZ = IST

ACTIONS = ("UNLOCK", "LOCK", "PANIC")

SHIFT_HOURS = {
    1: (0, 8),
    2: (8, 16),
    3: (16, 24),
}
SHIFTS = tuple(SHIFT_HOURS)
DEFAULT_REGION = "MAIN"


def normalize_phone(raw: str) -> str:
    """'+91 98123-45678' -> '919812345678'"""
    return re.sub(r"\D", "", raw or "")


def normalize_vehicle(raw: str) -> str:
    """'hrx-1234 ' -> 'HRX1234'"""
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


def _load_engineering_team() -> dict[str, str]:
    """
    ENGINEERING_TEAM_<ROLE>=<phone>[,<phone>] in .env -> {phone: "EngineeringTeam_<ROLE>"}.
    Adding a person or a role is one line in .env and no code change.
    """
    team = {}
    for key, value in os.environ.items():
        if not key.startswith("ENGINEERING_TEAM_"):
            continue
        role = "EngineeringTeam_" + key[len("ENGINEERING_TEAM_"):].upper()
        for raw in value.split(","):
            if normalize_phone(raw):
                team[normalize_phone(raw)] = role
    return team


ENGINEERING_TEAM: dict[str, str] = _load_engineering_team()


def looks_like_vehicle(text: str) -> bool:
    """Cheap filter so 'hi' doesn't get treated as a plate."""
    v = normalize_vehicle(text)
    if not re.fullmatch(r"[A-Z0-9]{5,13}", v):
        return False
    return any(c.isalpha() for c in v) and any(c.isdigit() for c in v)


GREETINGS = {
    "hi", "hii", "hiii", "hiiii", "hello", "helo", "hey", "heyy", "hai",
    "namaste", "namaskar", "salaam", "hey there", "hi there", "hello there",
    "howdy", "yo", "sup", "whats up", "good morning", "good afternoon",
    "good evening", "gm", "hola", "hi sir", "hello sir", "hi bot", "hello bot",
}


def looks_like_greeting(text: str) -> bool:
    """'Hello!!', 'hi sir', 'Good morning 🙏' all count."""
    words = re.sub(r"[^a-z ]", "", (text or "").lower())
    return " ".join(words.split()) in GREETINGS


def _truthy(value: str) -> bool:
    return (value or "").strip().lower() in {"yes", "y", "true", "1"}


def _atomic_write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    """Write to a temp file then swap, so a crash mid-write can't truncate the CSV."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# CSV store
# --------------------------------------------------------------------------
_managers_by_phone: dict[str, dict] = {}
_managers_by_id: dict[str, dict] = {}
_vehicles: dict[str, dict] = {}
_cras_by_id: dict[str, dict] = {}       # every ControlRoomAdmin, active or not (portal needs both)
_cras_by_phone: dict[str, dict] = {}    # active ControlRoomAdmins only
_data_lock = Lock()


def _read_cra_row(row: dict, line: int) -> dict:
    try:
        shift = int((row.get("shift") or "").strip())
    except ValueError:
        shift = 0
    if shift not in SHIFT_HOURS:
        raise ValueError(f"control_room_admins.csv line {line}: shift must be one of "
                         f"{SHIFTS}, got {row.get('shift')!r}")
    return {
        "cra_id": row["cra_id"].strip(),
        "name": row["name"].strip(),
        "phone": normalize_phone(row["phone"]),
        "shift": shift,
        "region": (row.get("region") or DEFAULT_REGION).strip().upper(),
        "active": _truthy(row.get("active", "yes")),
    }


def load_data() -> tuple[int, int, int]:
    """
    Read every CSV into memory. Called at import, startup, /health and after a
    portal save.

    Builds fresh dicts and swaps them in at the end: request handlers run
    concurrently, and clearing the live dicts first would make every fleet
    manager look unregistered for the duration of the reload.
    """
    managers_by_phone, managers_by_id, vehicles = {}, {}, {}
    cras_by_id, cras_by_phone = {}, {}

    with FM_CSV.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            fm = {
                "fm_id": row["fm_id"].strip(),
                "name": row["name"].strip(),
                "phone": normalize_phone(row["phone"]),
                "active": _truthy(row.get("active", "yes")),
            }
            managers_by_phone[fm["phone"]] = fm
            managers_by_id[fm["fm_id"]] = fm

    with VEHICLE_CSV.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            v = {
                "vehicle_no": normalize_vehicle(row["vehicle_no"]),
                "fm_id": row["fm_id"].strip(),
                "model": row.get("model", "").strip(),
                "active": _truthy(row.get("active", "yes")),
                "status": (row.get("status") or "LOCKED").strip().upper(),
            }
            vehicles[v["vehicle_no"]] = v

    if CONTROL_ROOM_CSV.exists():
        with CONTROL_ROOM_CSV.open(newline="", encoding="utf-8") as fh:
            for line, row in enumerate(csv.DictReader(fh), start=2):
                cra = _read_cra_row(row, line)
                cras_by_id[cra["cra_id"]] = cra
                if cra["active"]:
                    cras_by_phone[cra["phone"]] = cra

    global _managers_by_phone, _managers_by_id, _vehicles, _cras_by_id, _cras_by_phone
    with _data_lock:
        _managers_by_phone, _managers_by_id, _vehicles = managers_by_phone, managers_by_id, vehicles
        _cras_by_id, _cras_by_phone = cras_by_id, cras_by_phone

    return len(managers_by_id), len(vehicles), len(cras_by_id)


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
        _atomic_write_csv(VEHICLE_CSV, fields, rows)

    if vn in _vehicles:
        _vehicles[vn]["status"] = status


def get_fleet_manager(phone: str) -> dict | None:
    """Active FleetManager for this WhatsApp number, or None."""
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
# ControlRoomAdmins roster + shifts, EngineeringTeam
# --------------------------------------------------------------------------
def get_current_shift() -> int:
    hour = datetime.now(LOCAL_TZ).hour
    for shift, (start, end) in SHIFT_HOURS.items():
        if start <= hour < end:
            return shift
    return SHIFTS[0]


def shift_label(shift: int) -> str:
    start, end = SHIFT_HOURS[shift]
    return f"{start:02d}:00–{end:02d}:00"


def get_control_room_admins_for_shift(shift: int, region: str = DEFAULT_REGION) -> list[dict]:
    return [a for a in _cras_by_id.values()
            if a["active"] and a["shift"] == shift and a["region"] == region]


def get_control_room_admins_on_duty(region: str = DEFAULT_REGION) -> list[dict]:
    return get_control_room_admins_for_shift(get_current_shift(), region)


def get_control_room_admin(phone: str) -> dict | None:
    """Active ControlRoomAdmin for this number, or None."""
    return _cras_by_phone.get(normalize_phone(phone))


def is_engineering_team(phone: str) -> bool:
    return normalize_phone(phone) in ENGINEERING_TEAM


def engineering_team_role(phone: str) -> str:
    """'EngineeringTeam_CTO', or '' if not on the EngineeringTeam."""
    return ENGINEERING_TEAM.get(normalize_phone(phone), "")


def approver_name(phone: str) -> str:
    """How an approver is named in messages to others."""
    cra = get_control_room_admin(phone)
    if cra:
        return cra["name"]
    return engineering_team_role(phone) or phone


# --------------------------------------------------------------------------
# pending approvals
#
# One record per request, keyed by its Ref. The record — not the button
# payload — says which vehicle and action are being approved and who may
# approve it, and claiming it is atomic, so two approvers tapping Approve at the
# same moment produce exactly one vehicle command.
# --------------------------------------------------------------------------
_pending: dict[str, dict] = {}
_pending_lock = Lock()


def create_pending(ref: str, *, fm: dict, vehicle_no: str, action: str,
                   approvers: list[str], shift: int, escalated: bool) -> None:
    now = monotonic()
    with _pending_lock:
        for old in [r for r, p in _pending.items() if now - p["created"] > 86400]:
            _pending.pop(old, None)
        _pending[ref] = {
            "ref": ref,
            "fm_id": fm["fm_id"],
            "fm_name": fm["name"],
            "fm_phone": fm["phone"],
            "vehicle_no": vehicle_no,
            "action": action,
            "approvers": set(approvers),
            "shift": shift,
            "escalated": escalated,
            "status": "PENDING",
            "decided_by": "",
            "created": now,
            "expires": now + APPROVAL_TTL,
        }


def claim_pending(ref: str, phone: str, decision: str) -> tuple[dict | None, str]:
    """
    Atomically move a request from PENDING to `decision` (APPROVED/REJECTED).

    Returns (record, outcome) where outcome is one of:
      OK, UNKNOWN (no such ref, e.g. after a restart), NOT_APPROVER,
      EXPIRED, or the status it already has (APPROVED / REJECTED).
    """
    phone = normalize_phone(phone)
    with _pending_lock:
        p = _pending.get(ref)
        if not p:
            return None, "UNKNOWN"
        if phone not in p["approvers"]:
            return None, "NOT_APPROVER"
        if p["status"] != "PENDING":
            return dict(p), p["status"]
        if p["expires"] < monotonic():
            p["status"] = "EXPIRED"
            return dict(p), "EXPIRED"
        p["status"] = decision
        p["decided_by"] = phone
        return dict(p), "OK"


def expire_pending(ref: str) -> dict | None:
    """Mark as EXPIRED if still undecided. Returns the record if this call expired it."""
    with _pending_lock:
        p = _pending.get(ref)
        if not p or p["status"] != "PENDING":
            return None
        p["status"] = "EXPIRED"
        return dict(p)


def pending_for_fleet_manager(fm_phone: str) -> dict | None:
    """Most recent undecided request from this manager, if any."""
    fm_phone = normalize_phone(fm_phone)
    now = monotonic()
    with _pending_lock:
        live = [p for p in _pending.values()
                if p["fm_phone"] == fm_phone and p["status"] == "PENDING"
                and p["expires"] > now]
        return dict(max(live, key=lambda p: p["created"])) if live else None


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
# portal auth — WhatsApp OTP for the EngineeringTeam only, in-memory sessions
# --------------------------------------------------------------------------
OTP_TTL = 600            # seconds an OTP stays valid
OTP_MAX_ATTEMPTS = 5     # wrong guesses before the OTP is burned
OTP_RESEND_COOLDOWN = 60
PORTAL_SESSION_TTL = 12 * 3600

_otps: dict[str, dict] = {}
_portal_sessions: dict[str, dict] = {}
_auth_lock = Lock()


def issue_otp(phone: str) -> str | None:
    """New 6-digit OTP, or None if not on the EngineeringTeam or asked again too soon."""
    phone = normalize_phone(phone)
    if phone not in ENGINEERING_TEAM:
        return None
    now = monotonic()
    with _auth_lock:
        prev = _otps.get(phone)
        if prev and now - prev["issued"] < OTP_RESEND_COOLDOWN:
            return None
        otp = f"{secrets.randbelow(10**6):06d}"
        _otps[phone] = {"otp": otp, "issued": now, "expires": now + OTP_TTL, "attempts": 0}
        return otp


def login_with_otp(phone: str, otp: str) -> str | None:
    """Single-use OTP check. Returns a session token on success."""
    phone = normalize_phone(phone)
    with _auth_lock:
        rec = _otps.get(phone)
        if not rec or rec["expires"] < monotonic():
            _otps.pop(phone, None)
            return None
        rec["attempts"] += 1
        if not hmac.compare_digest(rec["otp"], (otp or "").strip()):
            if rec["attempts"] >= OTP_MAX_ATTEMPTS:
                _otps.pop(phone, None)
            return None
        _otps.pop(phone, None)
        token = secrets.token_urlsafe(32)
        _portal_sessions[token] = {"phone": phone, "expires": monotonic() + PORTAL_SESSION_TTL}
        return token


def portal_user(token: str) -> str | None:
    """Phone behind a valid session token, else None."""
    if not token:
        return None
    with _auth_lock:
        s = _portal_sessions.get(token)
        if not s:
            return None
        if s["expires"] < monotonic() or s["phone"] not in ENGINEERING_TEAM:
            _portal_sessions.pop(token, None)
            return None
        return s["phone"]


def logout_portal(token: str) -> None:
    with _auth_lock:
        _portal_sessions.pop(token, None)


# --------------------------------------------------------------------------
# portal data + roster writes
# --------------------------------------------------------------------------
_control_room_csv_lock = Lock()
CRA_FIELDS = ["cra_id", "name", "phone", "shift", "region", "active"]
_REGION_RE = re.compile(r"^[A-Z0-9_-]{1,32}$")


def portal_snapshot() -> dict:
    current = get_current_shift()
    return {
        "control_room_admins": [dict(a) for a in sorted(_cras_by_id.values(),
                                                        key=lambda a: a["cra_id"])],
        "shifts": [{"shift": s, "label": shift_label(s)} for s in SHIFTS],
        "current_shift": current,
        "timezone": TIMEZONE_STR,
        "server_time": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
    }


def update_control_room_roster(assignments: list[dict]) -> list[dict]:
    """
    Apply shift / region / active changes from the portal to control_room_admins.csv.

    Validates everything before touching the file: a bad value written here
    would make load_data() raise on the next start and take the bot down.
    Returns the ControlRoomAdmins whose assignment actually changed.
    """
    with _control_room_csv_lock:
        with CONTROL_ROOM_CSV.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        by_id = {r["cra_id"].strip(): r for r in rows}

        changes = []
        for a in assignments:
            if not isinstance(a, dict):
                raise ValueError("each assignment must be an object")
            cra_id = str(a.get("cra_id", "")).strip()
            if cra_id not in by_id:
                raise ValueError(f"unknown cra_id {cra_id!r}")
            try:
                shift = int(a.get("shift"))
            except (TypeError, ValueError):
                shift = 0
            if shift not in SHIFT_HOURS:
                raise ValueError(f"{cra_id}: shift must be one of {SHIFTS}")
            region = str(a.get("region") or DEFAULT_REGION).strip().upper()
            if not _REGION_RE.match(region):
                raise ValueError(f"{cra_id}: region must be letters/digits, max 32")
            active = a.get("active")
            if not isinstance(active, bool):
                raise ValueError(f"{cra_id}: active must be true/false")

            row = by_id[cra_id]
            new = {"shift": str(shift), "region": region, "active": "yes" if active else "no"}
            old = {"shift": row.get("shift", "").strip(),
                   "region": (row.get("region") or DEFAULT_REGION).strip().upper(),
                   "active": "yes" if _truthy(row.get("active", "yes")) else "no"}
            if new != old:
                row.update(new)
                changes.append({"cra_id": cra_id, "name": row["name"].strip(),
                                "before": old, "after": new})

        if changes:
            _atomic_write_csv(CONTROL_ROOM_CSV, CRA_FIELDS, rows)

    if changes:
        load_data()
    return changes


# --------------------------------------------------------------------------
# audit log — append-only, one row per stage
# --------------------------------------------------------------------------
AUDIT_FIELDS = [
    "ts_ist", "ref", "wamid", "phone", "fm_id", "vehicle_no",
    "action", "stage", "result", "actor", "detail", "http_status", "latency_ms", "error",
]
_audit_lock = Lock()


def _ensure_audit_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if AUDIT_CSV.exists():
        with AUDIT_CSV.open(newline="", encoding="utf-8") as fh:
            header = next(csv.reader(fh), [])
        if header == AUDIT_FIELDS:
            return
        # Columns changed: appending would misalign every new row under the old
        # header, so keep the old file intact under a dated name and start fresh.
        stamp = datetime.now(IST).strftime("%Y%m%d-%H%M%S")
        AUDIT_CSV.rename(AUDIT_CSV.with_name(f"audit_log.{stamp}.csv"))
    with AUDIT_CSV.open("w", newline="", encoding="utf-8") as fh:
        csv.DictWriter(fh, fieldnames=AUDIT_FIELDS).writeheader()


def log_audit(
    *, stage: str, result: str, phone: str = "", fm_id: str = "",
    vehicle_no: str = "", action: str = "", wamid: str = "", ref: str = "",
    actor: str = "", detail: str = "", http_status: str = "", latency_ms: str = "",
    error: str = "",
) -> None:
    """`phone` is the FleetManager's number; `actor` is whoever else acted (approver, EngineeringTeam)."""
    row = {
        "ts_ist": datetime.now(IST).isoformat(timespec="seconds"),
        "ref": ref, "wamid": wamid, "phone": phone, "fm_id": fm_id,
        "vehicle_no": vehicle_no, "action": action, "stage": stage,
        "result": result, "actor": actor, "detail": detail, "http_status": http_status,
        "latency_ms": latency_ms, "error": error,
    }
    with _audit_lock:
        _ensure_audit_file()
        with AUDIT_CSV.open("a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=AUDIT_FIELDS).writerow(row)
    print(f"[audit] {stage:<16} {result:<22} {phone or '-':<14} "
          f"{vehicle_no or '-':<10} {action or '-':<7} {actor}")


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
            print(f"[whatsapp] send to {payload.get('to')} failed "
                  f"{r.status_code}: {r.text[:300]}")
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
    Delivery/read 'statuses' events are handled by log_failed_deliveries.
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


def log_failed_deliveries(payload: dict) -> None:
    """
    Meta accepts a send with HTTP 200 and reports most failures later, as a
    'failed' status on this webhook. The common one here is 131047: the
    recipient hasn't messaged the bot in 24h, so only a template can reach
    them. Without this, those messages vanish silently.
    """
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for st in change.get("value", {}).get("statuses", []) or []:
                if st.get("status") != "failed":
                    continue
                for err in st.get("errors", []) or [{}]:
                    code = err.get("code", "?")
                    print(f"[whatsapp] DELIVERY FAILED to {st.get('recipient_id')}: "
                          f"{code} {err.get('title', '')} "
                          f"{(err.get('error_data') or {}).get('details', '')}")
                    log_audit(stage="DELIVERY", result=f"FAILED_{code}",
                              actor=st.get("recipient_id", ""),
                              error=str(err.get("title", ""))[:120])


# --------------------------------------------------------------------------
# vehicle control API  <- replace this for production
# --------------------------------------------------------------------------
async def call_vehicle_api(action: str, vehicle_no: str) -> dict:
    """
    Returns {ok, status, latency_ms, error}.

    Mock mode (no VEHICLE_API_BASE) succeeds MOCK_SUCCESS_RATE of the time so
    you can see both the success and the failure message without a device.
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
