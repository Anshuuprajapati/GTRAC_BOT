"""
main.py — webhook endpoints + the conversation state machine.

    IDLE ──vehicle no.──▶ AWAITING_ACTION ──tap action──▶ AWAITING_CONFIRM
      ▲                                                          │
      └──────────────── result / cancel / expiry ◀───────────────┘

Button payloads carry their own context ("ACT|UNLOCK|HRX1234"), so a restart
mid-conversation doesn't strand anyone.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from helpers import (
    ACTION_TTL,
    ACTIONS,
    CONFIRM_TTL,
    DRY_RUN,
    MOCK_VEHICLE_API,
    OUTBOX,
    REQUIRE_CONFIRM,
    STATUS_LABEL,
    VERIFY_TOKEN,
    already_seen,
    clear_session,
    get_manager,
    deny_reason,
    get_owned_vehicle,
    get_session,
    get_status,
    load_data,
    log_audit,
    looks_like_vehicle,
    new_ref,
    now_ist,
    normalize_vehicle,
    parse_incoming,
    read_audit,
    send_buttons,
    send_text,
    set_session,
    verify_signature,
)

@asynccontextmanager
async def lifespan(_: FastAPI):
    managers, vehicles = load_data()
    print(f"[startup] {managers} fleet managers, {vehicles} vehicles")
    if DRY_RUN:
        print("[startup] DRY_RUN — outgoing messages print to console")
    if MOCK_VEHICLE_API:
        print("[startup] MOCK vehicle API — ~85% success rate")
    yield


app = FastAPI(title="WhatsApp Vehicle Control Bot", lifespan=lifespan)


# --------------------------------------------------------------------------
# message copy
# --------------------------------------------------------------------------
LABEL = {"UNLOCK": "🔓 Unlock", "LOCK": "🔒 Lock", "PANIC": "🚨 Panic"}

CONFIRM_PROMPT = {
    "UNLOCK": "🔓 Unlock {v}?\nThis will unlock the vehicle doors.",
    "LOCK": "🔒 Lock {v}?\nThis will lock the vehicle doors.",
    "PANIC": (
        "🚨 Trigger panic alarm on {v}?\n\n"
        "The horn and lights will activate for 30 seconds. "
        "The driver will be alerted."
    ),
}
CONFIRM_BUTTON = {
    "UNLOCK": "✅ Yes, unlock",
    "LOCK": "✅ Yes, lock",
    "PANIC": "🚨 Yes, trigger",
}
SUCCESS_LINE = {
    "UNLOCK": "✅ {v} unlocked successfully",
    "LOCK": "✅ {v} locked successfully",
    "PANIC": "🚨 Panic alarm activated on {v}\nDuration: 30 seconds",
}

MSG_UNKNOWN_SENDER = (
    "❌ This number isn't registered as a fleet manager.\n\n"
    "Contact support to get access."
)
MSG_NOT_YOURS = (
    "❌ {v} is not linked to your account.\n\n"
    "Please check the vehicle number, or contact support "
    "if you think this is an error."
)
MSG_HELP = (
    "👋 Hi {name}!\n\n"
    "Send me a vehicle number to get started.\n\n"
    "Example: HRX1234\n\n"
    "I can unlock, lock, or trigger the panic alarm on your vehicles."
)
MSG_EXPIRED = "⏱️ This request has expired.\n\nPlease send the vehicle number again."
MSG_CANCELLED = "Cancelled. No action taken on {v}."

# Typed alternatives to tapping a button. Buttons are the happy path, but
# people reply with words, and on some clients the buttons don't render.
WORD_ACTION = {
    "unlock": "UNLOCK", "open": "UNLOCK", "1": "UNLOCK",
    "lock": "LOCK", "close": "LOCK", "2": "LOCK",
    "panic": "PANIC", "alarm": "PANIC", "horn": "PANIC",
    "panic sound": "PANIC", "panic button": "PANIC", "3": "PANIC",
}
WORD_YES = {"yes", "y", "yeah", "ok", "okay", "confirm", "sure", "haan", "ha"}
WORD_NO = {"no", "n", "cancel", "stop", "nahi", "abort"}


# --------------------------------------------------------------------------
# state machine
# --------------------------------------------------------------------------
async def handle_message(msg: dict) -> None:
    """One inbound WhatsApp message, start to finish."""
    try:
        await _handle_message(msg)
    except Exception as exc:
        # A crash here runs in a background task, where nobody sees it and the
        # manager just gets silence — after we may already have moved the
        # vehicle. Log loudly and always say something back.
        import traceback
        traceback.print_exc()
        log_audit(stage="HANDLER", result="CRASHED", phone=msg.get("phone", ""),
                  wamid=msg.get("wamid", ""), error=str(exc)[:120])
        try:
            await send_text(
                msg.get("phone", ""),
                "⚠️ Something went wrong on our end.\n\n"
                "Please check the vehicle before retrying, and contact support "
                "if this keeps happening.",
            )
        except Exception:
            pass


async def _handle_message(msg: dict) -> None:
    phone, wamid = msg["phone"], msg["wamid"]

    if wamid and already_seen(wamid):
        print(f"[dedupe] ignoring repeat of {wamid}")
        return

    fm = get_manager(phone)
    if not fm:
        log_audit(stage="REQUEST", result="DENIED_UNKNOWN_SENDER",
                  phone=phone, wamid=wamid)
        await send_text(phone, MSG_UNKNOWN_SENDER)
        return

    if msg["button_id"]:
        await _handle_button(fm, phone, wamid, msg["button_id"])
    elif msg["type"] == "text":
        await _handle_text(fm, phone, wamid, msg["text"])
    else:
        await send_text(phone, MSG_HELP.format(name=fm["name"]))


async def _handle_text(fm: dict, phone: str, wamid: str, text: str) -> None:
    """A typed vehicle number always wins — it resets whatever state they were in."""
    word = " ".join(text.lower().split())

    # A vehicle number takes priority over everything else.
    if looks_like_vehicle(text):
        vehicle_no = normalize_vehicle(text)
        vehicle = get_owned_vehicle(vehicle_no, fm["fm_id"])

        if not vehicle:
            log_audit(stage="REQUEST", result=deny_reason(vehicle_no, fm["fm_id"]),
                      phone=phone, fm_id=fm["fm_id"], vehicle_no=vehicle_no,
                      wamid=wamid)
            await send_text(phone, MSG_NOT_YOURS.format(v=vehicle_no))
            return

        log_audit(stage="REQUEST", result="VERIFIED", phone=phone,
                  fm_id=fm["fm_id"], vehicle_no=vehicle_no, wamid=wamid)
        set_session(phone, "AWAITING_ACTION", ACTION_TTL, vehicle=vehicle_no)

        model = f" — {vehicle['model']}" if vehicle["model"] else ""
        state = STATUS_LABEL.get(vehicle["status"], vehicle["status"])
        await send_buttons(
            phone,
            f"✅ {vehicle_no}{model}\nOwner: {fm['name']}\n"
            f"Status: {state}\n\nWhat would you like to do?",
            [(f"ACT|{a}|{vehicle_no}", LABEL[a]) for a in ACTIONS],
        )
        return

    # Otherwise, a typed word may stand in for a button tap.
    session = get_session(phone)
    if session:
        vehicle_no = session.get("vehicle", "")
        if session["state"] == "AWAITING_CONFIRM":
            action = session.get("action", "")
            if word in WORD_YES:
                await _handle_button(fm, phone, wamid, f"CONF|{action}|{vehicle_no}")
                return
            if word in WORD_NO:
                await _handle_button(fm, phone, wamid, f"CANCEL|{action}|{vehicle_no}")
                return
        elif session["state"] == "AWAITING_ACTION" and word in WORD_ACTION:
            await _handle_button(
                fm, phone, wamid, f"ACT|{WORD_ACTION[word]}|{vehicle_no}")
            return

    # A bare action word with no vehicle in play.
    if word in WORD_ACTION or word in WORD_YES or word in WORD_NO:
        await send_text(
            phone,
            "Send me the vehicle number first.\n\nExample: HRX1004")
        return

    await send_text(phone, MSG_HELP.format(name=fm["name"]))


async def _handle_button(fm: dict, phone: str, wamid: str, payload: str) -> None:
    parts = payload.split("|")
    if len(parts) != 3 or parts[1] not in ACTIONS:
        await send_text(phone, MSG_HELP.format(name=fm["name"]))
        return
    kind, action, vehicle_no = parts[0], parts[1], normalize_vehicle(parts[2])

    # Re-check ownership on every tap — a button payload is user-supplied input.
    if not get_owned_vehicle(vehicle_no, fm["fm_id"]):
        log_audit(stage="BUTTON", result=deny_reason(vehicle_no, fm["fm_id"]),
                  phone=phone, fm_id=fm["fm_id"], vehicle_no=vehicle_no,
                  action=action, wamid=wamid)
        await send_text(phone, MSG_NOT_YOURS.format(v=vehicle_no))
        return

    session = get_session(phone)

    if kind == "CANCEL":
        clear_session(phone)
        log_audit(stage="CONFIRMED", result="CANCELLED", phone=phone,
                  fm_id=fm["fm_id"], vehicle_no=vehicle_no, action=action,
                  wamid=wamid)
        await send_text(phone, MSG_CANCELLED.format(v=vehicle_no))
        return

    if kind == "ACT":
        if not session or session["state"] != "AWAITING_ACTION":
            await _expire(fm, phone, wamid, vehicle_no, action, "ACTION_SELECTED")
            return
        log_audit(stage="ACTION_SELECTED", result="PENDING", phone=phone,
                  fm_id=fm["fm_id"], vehicle_no=vehicle_no, action=action,
                  wamid=wamid)

        if not REQUIRE_CONFIRM.get(action, True):
            await _execute(fm, phone, wamid, vehicle_no, action)
            return

        set_session(phone, "AWAITING_CONFIRM", CONFIRM_TTL,
                    vehicle=vehicle_no, action=action)
        await send_buttons(
            phone,
            CONFIRM_PROMPT[action].format(v=vehicle_no),
            [
                (f"CONF|{action}|{vehicle_no}", CONFIRM_BUTTON[action]),
                (f"CANCEL|{action}|{vehicle_no}", "❌ Cancel"),
            ],
        )
        return

    if kind == "CONF":
        if not session or session["state"] != "AWAITING_CONFIRM":
            await _expire(fm, phone, wamid, vehicle_no, action, "CONFIRMED")
            return
        await _execute(fm, phone, wamid, vehicle_no, action)


async def _expire(fm, phone, wamid, vehicle_no, action, stage) -> None:
    clear_session(phone)
    log_audit(stage=stage, result="EXPIRED", phone=phone, fm_id=fm["fm_id"],
              vehicle_no=vehicle_no, action=action, wamid=wamid)
    await send_text(phone, MSG_EXPIRED)


async def _execute(fm: dict, phone: str, wamid: str,
                   vehicle_no: str, action: str) -> None:
    from helpers import call_vehicle_api  # imported here so it's easy to patch

    ref = new_ref()
    log_audit(stage="CONFIRMED", result="PENDING", phone=phone, fm_id=fm["fm_id"],
              vehicle_no=vehicle_no, action=action, wamid=wamid, ref=ref)

    verb = {"UNLOCK": "unlock", "LOCK": "lock", "PANIC": "panic"}[action]
    await send_text(phone, f"⏳ Sending {verb} command to {vehicle_no}…")

    result = await call_vehicle_api(action, vehicle_no)
    clear_session(phone)

    log_audit(
        stage="EXECUTED",
        result="SUCCESS" if result["ok"] else "FAILED",
        phone=phone, fm_id=fm["fm_id"], vehicle_no=vehicle_no, action=action,
        wamid=wamid, ref=ref,
        http_status=str(result["status"]),
        latency_ms=str(result["latency_ms"]),
        error=result["error"],
    )

    if result["ok"]:
        state = STATUS_LABEL.get(get_status(vehicle_no), "")
        await send_text(
            phone,
            f"{SUCCESS_LINE[action].format(v=vehicle_no)}\n\n"
            f"Action: {action.title()}\n"
            f"Status: {state}\n"
            f"Time:   {now_ist()}\nRef:    {ref}",
        )
    else:
        await send_text(
            phone,
            f"⚠️ Couldn't reach {vehicle_no}\n\n"
            "No action was taken. The vehicle may be offline or out of network.\n\n"
            f"Please try again, or contact support.\nRef: {ref}",
        )


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------
@app.get("/webhook")
def verify(request: Request):
    """Meta's one-time subscription handshake."""
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and \
       params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(params.get("hub.challenge", ""))
    return PlainTextResponse("forbidden", status_code=403)


@app.post("/webhook")
@app.post("/webhook/")   # Meta callback URLs with a trailing slash would
                         # otherwise 307-redirect and lose the signature header
async def webhook(request: Request, background: BackgroundTasks):
    """
    Return 200 immediately, then work in the background.

    Meta retries anything slow, and a retried panic is a second alarm — so the
    ack has to come before the vehicle call, not after.
    """
    raw = await request.body()
    if not verify_signature(raw, request.headers.get("x-hub-signature-256")):
        return Response(status_code=403)

    try:
        payload = await request.json()
    except Exception:
        return {"status": "ignored"}

    for msg in parse_incoming(payload):
        background.add_task(handle_message, msg)
    return {"status": "ok"}


@app.post("/simulate")
async def simulate(body: dict):
    """
    Local testing without WhatsApp.

        {"phone": "919812345678", "text": "HRX1234"}
        {"phone": "919812345678", "button": "ACT|UNLOCK|HRX1234"}

    Runs the real pipeline and returns whatever the bot would have sent.
    """
    start = len(OUTBOX)
    await handle_message({
        "wamid": body.get("wamid") or f"sim.{len(OUTBOX)}.{id(body)}",
        "phone": str(body.get("phone", "")),
        "type": "interactive" if body.get("button") else "text",
        "text": body.get("text", ""),
        "button_id": body.get("button", ""),
    })
    return {"replies": list(OUTBOX)[start:]}


@app.get("/audit")
def audit(limit: int = 50):
    return JSONResponse(read_audit(limit))


@app.get("/health")
def health():
    managers, vehicles = load_data()
    return {
        "status": "ok",
        "fleet_managers": managers,
        "vehicles": vehicles,
        "dry_run": DRY_RUN,
        "mock_vehicle_api": MOCK_VEHICLE_API,
    }