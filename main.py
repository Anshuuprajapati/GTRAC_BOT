"""
main.py — webhook endpoints, the conversation state machine, and the
/elockadmins roster portal.

FleetManager:
    IDLE ──vehicle no.──▶ AWAITING_ACTION ──tap action──▶ AWAITING_CONFIRM
      ▲                                                          │ confirm
      │                                                          ▼
      └──── approved+executed / rejected / expired ◀──── PENDING APPROVAL

Approvers: the ControlRoomAdmins on the current shift
(data/control_room_admins.csv). If none are on duty the request escalates to
the EngineeringTeam instead of going nowhere.

FleetManager button payloads carry their own context ("ACT|UNLOCK|HRX1234"),
so a restart mid-conversation doesn't strand anyone. Approver buttons carry
only the Ref ("APPROVE|REQ-1A2B3C"): what is being approved, and who may
approve it, lives server-side in the pending record so it can't be forged.
"""

from __future__ import annotations

import asyncio
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from helpers import (
    ACTION_TTL,
    ACTIONS,
    ESCALATION_TTL,
    HANDOFF_TTL,
    STAGE_CRA,
    STAGE_ENG,
    CONFIRM_TTL,
    DRY_RUN,
    ENABLE_SIMULATE,
    ENGINEERING_TEAM,
    MOCK_VEHICLE_API,
    OUTBOX,
    REQUIRE_CONFIRM,
    SHIFTS,
    STATUS_LABEL,
    VERIFY_TOKEN,
    add_control_room_admin,
    already_seen,
    approver_name,
    claim_pending,
    clear_session,
    create_pending,
    delete_control_room_admin,
    deny_reason,
    describe_holder,
    describe_phone,
    engineering_team_role,
    advance_pending,
    engineering_team_holders,
    get_pending,
    holder_from_cra,
    next_in_rotation,
    stage_ttl,
    get_control_room_admin,
    get_control_room_admins_for_shift,
    get_control_room_admins_on_duty,
    get_current_shift,
    get_fleet_manager,
    get_owned_vehicle,
    get_session,
    get_status,
    is_engineering_team,
    issue_otp,
    load_data,
    log_audit,
    log_failed_deliveries,
    login_with_otp,
    logout_portal,
    looks_like_greeting,
    looks_like_vehicle,
    new_ref,
    normalize_phone,
    normalize_vehicle,
    now_ist,
    parse_incoming,
    pending_for_fleet_manager,
    pending_status,
    portal_snapshot,
    portal_user,
    read_audit,
    send_buttons,
    send_text,
    set_session,
    shift_label,
    update_control_room_admin,
    verify_signature,
)

PORTAL_FILE = Path(__file__).resolve().parent / "portal.html"


@asynccontextmanager
async def lifespan(_: FastAPI):
    managers, vehicles, cras = load_data()
    shift = get_current_shift()
    print(f"[startup] {managers} FleetManagers, {vehicles} vehicles, {cras} ControlRoomAdmins")
    print(f"[startup] Shift {shift} ({shift_label(shift)}) active — "
          f"{len(get_control_room_admins_on_duty())} ControlRoomAdmin(s) on duty")
    for s in SHIFTS:
        if not get_control_room_admins_for_shift(s):
            print(f"[startup] WARNING: no active ControlRoomAdmins on shift {s} — "
                  "requests in that window escalate to the EngineeringTeam")
    roles = ", ".join(sorted(set(ENGINEERING_TEAM.values()))) or "none"
    print(f"[startup] EngineeringTeam (portal + fallback approvers): {roles}")
    if not ENGINEERING_TEAM:
        print("[startup] WARNING: no ENGINEERING_TEAM_<ROLE> in .env — nobody can use the portal")
    if DRY_RUN:
        print("[startup] DRY_RUN — outgoing messages print to console")
    if MOCK_VEHICLE_API:
        print("[startup] MOCK vehicle API")
    if ENABLE_SIMULATE and not DRY_RUN:
        print("[startup] WARNING: /simulate is ON with live credentials. Anyone "
              "who can reach this server can impersonate FleetManagers and approvers.")
    yield


app = FastAPI(title="WhatsApp Vehicle Control Bot", lifespan=lifespan)


# --------------------------------------------------------------------------
# message copy
# --------------------------------------------------------------------------
LABEL = {"UNLOCK": "🔓 Unlock", "LOCK": "🔒 Lock", "PANIC": "🚨 Panic"}
VERB = {"UNLOCK": "unlock", "LOCK": "lock", "PANIC": "trigger panic on"}

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
    "Contact support to get access.\n\n"
    "info@gtrac.in\n\n"
    "Phone: +919999999999"
)
MSG_NOT_YOURS = (
    "❌ {v} is not linked to your account.\n\n"
    "Please check the vehicle number, or contact support "
    "if you think this is an error."
)
MSG_GREETING = (
    "👋 Hello {name}!\n\n"
    "Please send the vehicle number you want to manage.\n\n"
    "Example: HRX1234\n\n"
    "I can unlock, lock, or trigger the panic alarm."
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

APPROVER_BUTTONS = {"APPROVE": "APPROVED", "REJECT": "REJECTED"}


# --------------------------------------------------------------------------
# state machine
# --------------------------------------------------------------------------
async def handle_message(msg: dict) -> None:
    """One inbound WhatsApp message, start to finish."""
    try:
        await _handle_message(msg)
    except Exception as exc:
        # A crash here runs in a background task, where nobody sees it and the
        # sender just gets silence — after we may already have moved the
        # vehicle. Log loudly and always say something back.
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
    phone, wamid = normalize_phone(msg["phone"]), msg["wamid"]

    if wamid and already_seen(wamid):
        print(f"[dedupe] ignoring repeat of {wamid}")
        return

    # Approver taps are routed before the FleetManager check: approvers are
    # usually not FleetManagers, and would otherwise be told they're unregistered.
    if msg["button_id"].split("|", 1)[0] in APPROVER_BUTTONS:
        await _handle_approver_button(phone, wamid, msg["button_id"])
        return

    fm = get_fleet_manager(phone)
    if not fm:
        if get_control_room_admin(phone) or is_engineering_team(phone):
            await _reply_to_staff(phone)
            return
        log_audit(stage="REQUEST", result="DENIED_UNKNOWN_SENDER",
                  phone=phone, wamid=wamid)
        await send_text(phone, MSG_UNKNOWN_SENDER)
        return

    if msg["button_id"]:
        await _handle_button(fm, phone, wamid, msg["button_id"])
    elif msg["type"] == "text":
        await _handle_text(fm, phone, wamid, msg["text"])
    else:
        await send_text(phone, MSG_GREETING.format(name=fm["name"]))


async def _reply_to_staff(phone: str) -> None:
    """
    A ControlRoomAdmin or EngineeringTeam member messaged the bot. Besides
    answering, this matters for delivery: WhatsApp only allows free-form
    messages (like approval requests) to someone who has messaged the bot
    within the last 24 hours.
    """
    cra = get_control_room_admin(phone)
    lines = []
    if cra:
        on = "✅ You are ON DUTY now." if cra["shift"] == get_current_shift() else \
             "You are off duty right now."
        lines += [f"👋 Hi {cra['name']} (ControlRoomAdmin).",
                  f"Roster: Shift {cra['shift']} ({shift_label(cra['shift'])}), "
                  f"region {cra['region']}.",
                  on,
                  "Approval requests for your shift arrive here."]
    role = engineering_team_role(phone)
    if role:
        lines += ["", f"You are {role}. You manage the ControlRoomAdmins roster at "
                      "/elockadmins and receive requests when no one is on shift."]
    lines += ["", "Tip: send any message at the start of each shift so WhatsApp "
                  "keeps delivering requests to you."]
    await send_text(phone, "\n".join(lines).strip())


async def _handle_text(fm: dict, phone: str, wamid: str, text: str) -> None:
    """A typed vehicle number always wins — it resets whatever state they were in."""
    word = " ".join(text.lower().split())

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

    # A typed word may stand in for a button tap.
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

    waiting = pending_for_fleet_manager(phone)
    if waiting and (word in WORD_ACTION or word in WORD_YES or word in WORD_NO):
        await send_text(
            phone,
            f"⏳ Still waiting for approval to {VERB[waiting['action']]} "
            f"{waiting['vehicle_no']}.\nRef: {waiting['ref']}")
        return

    if word in WORD_ACTION or word in WORD_YES or word in WORD_NO:
        await send_text(phone, "Send me the vehicle number first.\n\nExample: HRX1004")
        return

    # Greetings and anything else unrecognised get the same friendly prompt.
    if not looks_like_greeting(text):
        log_audit(stage="REQUEST", result="UNRECOGNISED_TEXT", phone=phone,
                  fm_id=fm["fm_id"], wamid=wamid)
    await send_text(phone, MSG_GREETING.format(name=fm["name"]))


async def _handle_button(fm: dict, phone: str, wamid: str, payload: str) -> None:
    parts = payload.split("|")
    if len(parts) != 3 or parts[1] not in ACTIONS:
        await send_text(phone, MSG_GREETING.format(name=fm["name"]))
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
        if (not session or session["state"] != "AWAITING_ACTION"
                or session.get("vehicle") != vehicle_no):
            await _expire(fm, phone, wamid, vehicle_no, action, "ACTION_SELECTED")
            return
        log_audit(stage="ACTION_SELECTED", result="PENDING", phone=phone,
                  fm_id=fm["fm_id"], vehicle_no=vehicle_no, action=action,
                  wamid=wamid)

        if not REQUIRE_CONFIRM.get(action, True):
            await _request_approval(fm, phone, wamid, vehicle_no, action)
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
        # The session must match the payload, or an old Confirm button for a
        # different action/vehicle could ride on today's session.
        if (not session or session["state"] != "AWAITING_CONFIRM"
                or session.get("vehicle") != vehicle_no
                or session.get("action") != action):
            await _expire(fm, phone, wamid, vehicle_no, action, "CONFIRMED")
            return
        await _request_approval(fm, phone, wamid, vehicle_no, action)


async def _expire(fm, phone, wamid, vehicle_no, action, stage) -> None:
    clear_session(phone)
    log_audit(stage=stage, result="EXPIRED", phone=phone, fm_id=fm["fm_id"],
              vehicle_no=vehicle_no, action=action, wamid=wamid)
    await send_text(phone, MSG_EXPIRED)


# --------------------------------------------------------------------------
# approval
# --------------------------------------------------------------------------
_timers: set[asyncio.Task] = set()  # strong refs, or the event loop may GC them


def _minutes(seconds: int) -> str:
    return f"{seconds // 60} min" if seconds >= 60 and seconds % 60 == 0 else f"{seconds} sec"


def _approval_body(p: dict, note: str) -> str:
    shift_part = "ESCALATED to EngineeringTeam" if p["stage"] == STAGE_ENG else f"Shift {p['shift']}"
    if p["stage"] == STAGE_ENG:
        deadline = f"Please answer within {_minutes(ESCALATION_TTL)}, or the request expires."
    else:
        deadline = (f"Please answer within {_minutes(HANDOFF_TTL)}. After that it passes "
                    "to the next ControlRoomAdmin on your shift.")
    turn = (f"Your turn: {p['holders'][0]['name']} ({p['holders'][0]['cra_id']})\n"
            if p["stage"] == STAGE_CRA else "")
    return (f"🔔 Approval request — {shift_part}\n"
            + turn + (f"{note}\n" if note else "") +
            f"\nFleetManager: {p['fm_name']} ({p['fm_phone']})\n"
            f"Vehicle: {p['vehicle_no']}\n"
            f"Action: {LABEL[p['action']]}\n"
            f"Sent: {now_ist()}\n\n"
            f"{deadline}\n\nRef: {p['ref']}")


def _holder_ids(holders: list[dict]) -> str:
    """CRA ids for ControlRoomAdmins, phone numbers for the EngineeringTeam (audit detail)."""
    return ",".join(h["cra_id"] or h["phone"] for h in holders)


def _names(holders: list[dict]) -> str:
    return ", ".join(h["name"] for h in holders)


async def _send_to_holders(p: dict, note: str = "") -> None:
    body = _approval_body(p, note)
    for phone in sorted(p["approvers"]):
        await send_buttons(phone, body, [
            (f"APPROVE|{p['ref']}", "✅ Approve"),
            (f"REJECT|{p['ref']}", "❌ Reject"),
        ])


async def _request_approval(fm: dict, phone: str, wamid: str,
                            vehicle_no: str, action: str) -> None:
    clear_session(phone)
    ref = new_ref()
    shift = get_current_shift()
    first = next_in_rotation(shift)
    if first:
        holders, stage = [holder_from_cra(first)], STAGE_CRA
    else:
        holders, stage = engineering_team_holders(), STAGE_ENG

    if not holders:
        log_audit(stage="APPROVAL", result="NO_APPROVER", phone=phone, fm_id=fm["fm_id"],
                  vehicle_no=vehicle_no, action=action, wamid=wamid, ref=ref)
        await send_text(phone, "⚠️ No approver is available right now, so no action "
                               f"was taken on {vehicle_no}.\n\nPlease contact support.\nRef: {ref}")
        return

    p = create_pending(ref, fm=fm, vehicle_no=vehicle_no, action=action,
                       holders=holders, stage=stage, shift=shift)
    log_audit(stage="APPROVAL",
              result="REQUESTED" if stage == STAGE_CRA else "ESCALATED_TO_ENGINEERING_TEAM",
              phone=phone, fm_id=fm["fm_id"], vehicle_no=vehicle_no, action=action,
              wamid=wamid, ref=ref, actor=holders[0]["phone"] if stage == STAGE_CRA else "",
              detail=(f"shift {shift}, turn of {first['name']} ({first['cra_id']})" if first else
                      f"shift {shift}, no ControlRoomAdmin on duty")
                     + f"; approvers {_holder_ids(holders)}")

    await send_text(
        phone,
        f"⏳ Your request to {VERB[action]} {vehicle_no} has been sent for approval.\n\n"
        f"You'll get a message here as soon as it's approved or rejected.\nRef: {ref}")

    await _send_to_holders(p, "" if first else "No ControlRoomAdmin is on duty.")

    task = asyncio.create_task(_approval_clock(ref))
    _timers.add(task)
    task.add_done_callback(_timers.discard)


async def _approval_clock(ref: str) -> None:
    """Pass an unanswered request along the shift, then up to the EngineeringTeam, then expire."""
    p = get_pending(ref)
    while p and p["status"] == "PENDING":
        await asyncio.sleep(stage_ttl(p))
        step = advance_pending(ref)
        if not step:
            return  # decided while we waited
        kind, p = step
        prev = p["previous"]
        prev_names = _names(prev)
        prev_phones = sorted({h["phone"] for h in prev})
        common = dict(phone=p["fm_phone"], fm_id=p["fm_id"], vehicle_no=p["vehicle_no"],
                      action=p["action"], ref=ref)
        waited = _minutes(HANDOFF_TTL if prev and prev[0]["cra_id"] else ESCALATION_TTL)

        if kind == "EXPIRED":
            log_audit(stage="APPROVAL", result="EXPIRED", **common,
                      detail=f"no answer from {prev_names or 'anyone'}")
            await send_text(p["fm_phone"],
                            f"⏱️ No approver responded in time, so no action was taken on "
                            f"{p['vehicle_no']}.\n\nSend the vehicle number to try again.\nRef: {ref}")
            for x in prev_phones:
                await send_text(x, f"⌛ {ref} expired unanswered ({p['action']} "
                                   f"{p['vehicle_no']}). No action was taken.")
            return

        if kind == "HANDOFF":
            new = p["holders"][0]
            log_audit(stage="APPROVAL", result="HANDED_OFF", **common, actor=new["phone"],
                      detail=f"no answer from {prev_names} in {waited}, now {new['name']} "
                             f"({new['cra_id']}); approvers {_holder_ids(p['holders'])}")
            note = f"Passed on: {prev_names} didn't answer in {waited}."
            moved_to = new["name"]
        else:
            log_audit(stage="APPROVAL", result="ESCALATED_TO_ENGINEERING_TEAM", **common,
                      detail=f"no answer from anyone on shift {p['shift']} "
                             f"(last: {prev_names}); approvers {_holder_ids(p['holders'])}")
            note = f"Nobody on Shift {p['shift']} answered (last: {prev_names})."
            moved_to = "the EngineeringTeam"
        for x in prev_phones:
            await send_text(x, f"⏭️ {ref}: {prev_names} didn't answer in {waited}, so it "
                               f"passed to {moved_to}. {prev_names}'s buttons for it no "
                               "longer work.")
        await _send_to_holders(p, note)


async def _handle_approver_button(phone: str, wamid: str, payload: str) -> None:
    parts = payload.split("|")
    if len(parts) != 2 or parts[0] not in APPROVER_BUTTONS:
        await send_text(phone, "That button is no longer valid.")
        return
    ref = parts[1]
    decision = APPROVER_BUTTONS[parts[0]]

    # Someone removed or benched in the portal after the request went out keeps
    # the WhatsApp buttons, but must lose the power to use them.
    if not (get_control_room_admin(phone) or is_engineering_team(phone)):
        log_audit(stage="APPROVAL", result="DENIED_NOT_ON_ROSTER", ref=ref, actor=phone,
                  wamid=wamid)
        await send_text(phone, "❌ You're no longer on the approver roster, so this "
                               "button can't be used.")
        return

    p, outcome = claim_pending(ref, phone, decision)

    if outcome == "UNKNOWN":
        log_audit(stage="APPROVAL", result="UNKNOWN_REF", ref=ref, actor=phone, wamid=wamid)
        await send_text(phone, f"{ref} is no longer active (the service may have "
                               "restarted). No action was taken. The FleetManager will "
                               "need to send the request again.")
        return
    if outcome == "NOT_APPROVER":
        log_audit(stage="APPROVAL", result="DENIED_NOT_APPROVER", ref=ref, actor=phone,
                  wamid=wamid)
        await send_text(phone, "❌ You aren't an approver for this request.")
        return
    if outcome == "EXPIRED":
        await send_text(phone, f"⌛ {ref} has expired. No action was taken.")
        return
    if outcome == "PASSED_ON":
        log_audit(stage="APPROVAL", result="DENIED_PASSED_ON", ref=ref, actor=phone, wamid=wamid)
        await send_text(phone, f"⏭️ Too late: {ref} was already passed to "
                               f"{_names(p['holders'])} because it wasn't answered in time. "
                               "No action was taken.")
        return
    if outcome != "OK":
        await send_text(phone, f"ℹ️ {ref} was already {outcome.lower()} by "
                               f"{p['decided_by_name'] or approver_name(p['decided_by'])}. "
                               "Nothing more to do.")
        return

    name = p["decided_by_name"]
    fm_phone, vehicle_no, action = p["fm_phone"], p["vehicle_no"], p["action"]
    common = dict(phone=fm_phone, fm_id=p["fm_id"], vehicle_no=vehicle_no,
                  action=action, ref=ref, actor=phone, wamid=wamid)

    others = p["approvers"] - {phone}
    for other in others:
        await send_text(other, f"ℹ️ {ref} ({action} {vehicle_no}) was "
                               f"{decision.lower()} by {name}. No action needed.")

    if decision == "REJECTED":
        log_audit(stage="APPROVAL", result="REJECTED", **common)
        await send_text(fm_phone, f"❌ Your request to {VERB[action]} {vehicle_no} "
                                  f"was rejected.\n\nRef: {ref}")
        await send_text(phone, f"Rejected {ref}. {p['fm_name']} has been told.")
        return

    log_audit(stage="APPROVAL", result="APPROVED", **common)

    # The FleetManager or vehicle may have been deactivated or reassigned since the request.
    fm = get_fleet_manager(fm_phone)
    if not fm or not get_owned_vehicle(vehicle_no, fm["fm_id"]):
        log_audit(stage="EXECUTED", result="ABORTED_NOT_OWNED", **common)
        await send_text(phone, f"⚠️ {ref} not executed: {vehicle_no} is no longer "
                               f"linked to {p['fm_name']}.")
        await send_text(fm_phone, MSG_NOT_YOURS.format(v=vehicle_no))
        return

    await send_text(fm_phone, f"✅ Approved. Sending {action.lower()} command to {vehicle_no}…")
    await _execute(p, approver_phone=phone, wamid=wamid)


async def _execute(p: dict, *, approver_phone: str, wamid: str) -> None:
    from helpers import call_vehicle_api  # imported here so it's easy to patch

    fm_phone, vehicle_no, action, ref = p["fm_phone"], p["vehicle_no"], p["action"], p["ref"]
    result = await call_vehicle_api(action, vehicle_no)

    log_audit(
        stage="EXECUTED", result="SUCCESS" if result["ok"] else "FAILED",
        phone=fm_phone, fm_id=p["fm_id"], vehicle_no=vehicle_no, action=action,
        wamid=wamid, ref=ref, actor=approver_phone,
        http_status=str(result["status"]), latency_ms=str(result["latency_ms"]),
        error=result["error"],
    )

    if result["ok"]:
        state = STATUS_LABEL.get(get_status(vehicle_no), "")
        await send_text(
            fm_phone,
            f"{SUCCESS_LINE[action].format(v=vehicle_no)}\n\n"
            f"Action: {action.title()}\n"
            f"Status: {state}\n"
            f"Time:   {now_ist()}\nRef:    {ref}",
        )
        await send_text(approver_phone, f"✅ {ref} done: {action} on {vehicle_no} succeeded.")
    else:
        await send_text(
            fm_phone,
            f"⚠️ Couldn't reach {vehicle_no}\n\n"
            "No action was taken. The vehicle may be offline or out of network.\n\n"
            f"Please try again, or contact support.\nRef: {ref}",
        )
        await send_text(approver_phone, f"⚠️ {ref}: {action} on {vehicle_no} FAILED "
                                        f"({result['error']}). The FleetManager has been told.")


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

    log_failed_deliveries(payload)
    for msg in parse_incoming(payload):
        background.add_task(handle_message, msg)
    return {"status": "ok"}


@app.post("/simulate")
async def simulate(body: dict):
    """
    Local testing without WhatsApp. Disabled unless ENABLE_SIMULATE is on
    (default: on only in DRY_RUN), because it bypasses the Meta signature.

        {"phone": "918882374849", "text": "HRX1001"}
        {"phone": "918882374849", "button": "ACT|UNLOCK|HRX1001"}
        {"phone": "<approver>",   "button": "APPROVE|REQ-XXXXXX"}

    Runs the real pipeline and returns whatever the bot sent during the call.
    """
    if not ENABLE_SIMULATE:
        return JSONResponse({"error": "not found"}, status_code=404)
    start = len(OUTBOX)
    await handle_message({
        "wamid": body.get("wamid") or f"sim.{new_ref()}",
        "phone": str(body.get("phone", "")),
        "type": "interactive" if body.get("button") else "text",
        "text": body.get("text", ""),
        "button_id": body.get("button", ""),
    })
    return {"replies": list(OUTBOX)[start:]}


@app.get("/health")
def health():
    managers, vehicles, cras = load_data()
    return {
        "status": "ok",
        "fleet_managers": managers,
        "vehicles": vehicles,
        "control_room_admins": cras,
        "current_shift": get_current_shift(),
        "control_room_admins_on_duty": len(get_control_room_admins_on_duty()),
        "dry_run": DRY_RUN,
        "mock_vehicle_api": MOCK_VEHICLE_API,
    }


# --------------------------------------------------------------------------
# /elockadmins — ControlRoomAdmins roster portal, EngineeringTeam only
# --------------------------------------------------------------------------
def _portal_auth(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else ""
    return portal_user(token)


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "Please sign in again."}, status_code=401)


@app.get("/elockadmins")
def portal_page():
    # Read per request with an explicit encoding: Windows' default (cp1252)
    # can't decode the emoji in the file, which crashed startup before.
    html = PORTAL_FILE.read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store",
                                       "X-Frame-Options": "DENY"})


@app.get("/elockadmins/")
def portal_redirect():
    return RedirectResponse("/elockadmins")


@app.post("/api/portal/request-otp")
async def request_otp(body: dict):
    phone = normalize_phone(str(body.get("phone", "")))
    otp = issue_otp(phone)
    if otp:
        log_audit(stage="PORTAL", result="OTP_SENT", actor=phone)
        await send_text(phone, f"🔐 Your E-Lock roster portal code is {otp}\n\n"
                               "It expires in 10 minutes. Don't share it with anyone.")
    # Same answer either way, so the form can't be used to discover who is on the EngineeringTeam.
    return {"ok": True, "message": "If this number is authorised, a code has been "
                                   "sent on WhatsApp. You can request a new one after 60 seconds."}


@app.post("/api/portal/verify-otp")
async def verify_otp(body: dict):
    phone = normalize_phone(str(body.get("phone", "")))
    token = login_with_otp(phone, str(body.get("otp", "")))
    if not token:
        log_audit(stage="PORTAL", result="LOGIN_FAILED", actor=phone)
        return JSONResponse({"error": "That code is wrong or has expired."}, status_code=401)
    log_audit(stage="PORTAL", result="LOGIN", actor=phone)
    return {"ok": True, "token": token}


def _portal_payload(me: str) -> dict:
    return {**portal_snapshot(), "me": me, "role": engineering_team_role(me)}


def _bad_request(exc: Exception) -> JSONResponse:
    status = 404 if isinstance(exc, LookupError) else 400
    return JSONResponse({"error": str(exc).strip("'\"")}, status_code=status)


def _describe_cra(c: dict) -> str:
    state = "active" if c["active"] else "off roster"
    return f"+{c['phone']}, shift {c['shift']}, {c['region']}, {state}"


def _describe_diff(before: dict, after: dict) -> str:
    labels = {"name": "name", "phone": "phone", "shift": "shift",
              "region": "region", "active": "status"}
    def show(k, v):
        if k == "active":
            return "active" if v else "off roster"
        return f"+{v}" if k == "phone" else str(v)
    return ", ".join(f"{labels[k]} {show(k, before[k])} → {show(k, after[k])}"
                     for k in labels if before[k] != after[k])


@app.get("/api/portal/data")
def portal_data(request: Request):
    me = _portal_auth(request)
    if not me:
        return _unauthorized()
    return _portal_payload(me)


@app.post("/api/portal/cra")
async def portal_add_cra(request: Request, body: dict):
    me = _portal_auth(request)
    if not me:
        return _unauthorized()
    try:
        cra = add_control_room_admin(body)
    except ValueError as exc:
        return _bad_request(exc)
    log_audit(stage="PORTAL", result="CRA_ADDED", actor=me,
              detail=f"{cra['cra_id']} {cra['name']}: {_describe_cra(cra)}")
    return {"ok": True, "cra": cra, **_portal_payload(me)}


@app.patch("/api/portal/cra/{cra_id}")
async def portal_update_cra(cra_id: str, request: Request, body: dict):
    me = _portal_auth(request)
    if not me:
        return _unauthorized()
    try:
        before, after = update_control_room_admin(cra_id, body)
    except (ValueError, LookupError) as exc:
        return _bad_request(exc)
    diff = _describe_diff(before, after)
    if diff:
        log_audit(stage="PORTAL", result="CRA_UPDATED", actor=me,
                  detail=f"{cra_id} {after['name']}: {diff}")
    return {"ok": True, "changed": bool(diff), "cra": after, **_portal_payload(me)}


@app.delete("/api/portal/cra/{cra_id}")
async def portal_delete_cra(cra_id: str, request: Request):
    me = _portal_auth(request)
    if not me:
        return _unauthorized()
    try:
        cra = delete_control_room_admin(cra_id)
    except LookupError as exc:
        return _bad_request(exc)
    log_audit(stage="PORTAL", result="CRA_DELETED", actor=me,
              detail=f"{cra_id} {cra['name']}: was {_describe_cra(cra)}")
    return {"ok": True, **_portal_payload(me)}


_ROSTER_RESULTS = {"ROSTER_CHANGED", "CRA_ADDED", "CRA_UPDATED", "CRA_DELETED"}


def _activity_kind(row: dict) -> str:
    if row.get("stage") == "PORTAL":
        return "roster" if row.get("result") in _ROSTER_RESULTS else "signin"
    if row.get("stage") == "DELIVERY":
        return "delivery"
    return "request"


@app.get("/api/portal/activity")
def portal_activity(request: Request, limit: int = 400):
    """Newest-first audit trail with names resolved, for the Activity view and side panel."""
    if not _portal_auth(request):
        return _unauthorized()
    out = []
    for r in reversed(read_audit(20000)):
        r = {k: (v or "") for k, v in r.items()}
        out.append({
            "ts": r.get("ts_ist", ""), "kind": _activity_kind(r),
            "stage": r.get("stage", ""), "result": r.get("result", ""),
            "ref": r.get("ref", ""), "vehicle_no": r.get("vehicle_no", ""),
            "action": r.get("action", ""), "fm": describe_phone(r.get("phone", ""), as_fleet_manager=True),
            "actor": r.get("actor", ""), "actor_name": describe_phone(r.get("actor", "")),
            "detail": r.get("detail", ""), "error": r.get("error", ""),
        })
        if len(out) >= max(1, min(limit, 2000)):
            break
    return {"items": out}


@app.get("/api/portal/requests")
def portal_requests(request: Request, limit: int = 300):
    """One row per FleetManager request (Ref), rebuilt from the audit log."""
    if not _portal_auth(request):
        return _unauthorized()
    reqs: dict[str, dict] = {}
    for r in read_audit(50000):
        r = {k: (v or "") for k, v in r.items()}
        ref = r.get("ref", "")
        if not ref.startswith("REQ-"):
            continue
        q = reqs.setdefault(ref, {
            "ref": ref, "created": r["ts_ist"], "updated": r["ts_ist"],
            "fm_phone": "", "vehicle_no": "", "action": "", "status": "PENDING",
            "escalated": False, "sent_to_phones": [], "handoffs": 0, "decided_by": "", "decided_at": "",
            "result": "", "error": "", "latency_ms": "", "via_approval": False,
        })
        q["updated"] = r["ts_ist"]
        for k_src, k_dst in (("phone", "fm_phone"), ("vehicle_no", "vehicle_no"), ("action", "action")):
            if r.get(k_src) and not q[k_dst]:
                q[k_dst] = r[k_src]
        stage, res = r.get("stage"), r.get("result")
        if stage == "APPROVAL":
            q["via_approval"] = True
            if res in ("REQUESTED", "HANDED_OFF", "ESCALATED_TO_ENGINEERING_TEAM", "ESCALATED_TO_MVP"):
                q["escalated"] = q["escalated"] or res.startswith("ESCALATED")
                q["handoffs"] += res == "HANDED_OFF"
                detail = r.get("detail", "")
                if "approvers" in detail:
                    for ph in detail.split("approvers", 1)[1].split(","):
                        if ph.strip() and ph.strip() not in q["sent_to_phones"]:
                            q["sent_to_phones"].append(ph.strip())
            elif res in ("APPROVED", "REJECTED", "EXPIRED", "NO_APPROVER"):
                q["status"] = res
                if res in ("APPROVED", "REJECTED"):
                    q["decided_by"], q["decided_at"] = r.get("actor", ""), r["ts_ist"]
        elif stage == "EXECUTED":
            q["result"], q["error"], q["latency_ms"] = res, r.get("error", ""), r.get("latency_ms", "")

    out = []
    # A Ref with no FleetManager is just a stray tap on a dead button, not a request.
    real = [q for q in reqs.values() if q["fm_phone"]]
    for q in sorted(real, key=lambda q: q["created"], reverse=True)[:max(1, min(limit, 2000))]:
        q["sent_to"] = [describe_holder(t) for t in q.pop("sent_to_phones")]
        if not q["via_approval"]:
            q["status"] = "LEGACY"          # from before approvals existed
        elif q["status"] == "PENDING":
            live = pending_status(q["ref"])
            q["status"] = live if live in ("PENDING", "EXPIRED") else "INTERRUPTED"
        q["fm"] = describe_phone(q["fm_phone"], as_fleet_manager=True)
        q["decided_by_name"] = describe_phone(q["decided_by"])
        out.append(q)
    return {"items": out}


@app.post("/api/portal/logout")
def portal_logout(request: Request):
    header = request.headers.get("authorization", "")
    logout_portal(header[7:] if header.lower().startswith("bearer ") else "")
    return {"ok": True}


@app.get("/audit")
def audit(request: Request, limit: int = 50):
    # Contains every FleetManager's phone number and vehicle history; EngineeringTeam only.
    if not _portal_auth(request):
        return _unauthorized()
    return JSONResponse(read_audit(max(1, min(limit, 1000))))
