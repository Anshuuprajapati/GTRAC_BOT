# WhatsApp Vehicle Control Bot

Fleet managers message a vehicle number, tap an action, confirm. Replaces the
manual "message support to unlock a truck" loop.

```
WhatsApp → Meta Cloud API → FastAPI webhook → verify → action → Vehicle API → reply
                                                 └──────── audit_log.csv ────────┘
```

```
whatsapp-vehicle-bot/
├── main.py       webhook endpoints + conversation state machine
├── helpers.py    CSV store, sessions, WhatsApp send, vehicle API, audit
├── .env.example
├── requirements.txt
└── data/
    ├── fleet_managers.csv   5 managers
    ├── vehicles.csv         50 vehicles (10 each)
    └── audit_log.csv        auto-created, append-only
```

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload --workers 1
```

With `.env` left blank it runs fully offline: replies print to the console and
the vehicle API is simulated. You can test the whole flow before you have a
Meta account.

## Test without WhatsApp

`POST /simulate` runs the real pipeline and returns what the bot would send.

```bash
curl -s localhost:8000/simulate -H 'Content-Type: application/json' \
     -d '{"phone":"919812345678","text":"HRX1234"}'

curl -s localhost:8000/simulate -H 'Content-Type: application/json' \
     -d '{"phone":"919812345678","button":"ACT|UNLOCK|HRX1234"}'

curl -s localhost:8000/simulate -H 'Content-Type: application/json' \
     -d '{"phone":"919812345678","button":"CONF|UNLOCK|HRX1234"}'
```

Other endpoints: `GET /health`, `GET /audit?limit=50`.

### Test data

| Manager | Phone | Vehicles |
|---|---|---|
| Rajesh Kumar | 919811100001 | HRX1001–HRX1010 |
| Sarvesh Transport | 919812345678 | HRX1234, HRX5678, HRX9012, HRX2001–HRX2007 |
| Anita Sharma | 919833300003 | HRX3001–HRX3010 |
| Vikram Singh | 919844400004 | HRX4001–HRX4010 |
| Priya Logistics | 919855500005 | HRX5001–HRX5010 |

Useful cases: `HRX3001` from Sarvesh (other fleet), `HRX9999` (no such
vehicle), any plate from `919000000000` (unknown sender).

## Connect to Meta

1. Meta app → WhatsApp → API Setup. Copy the temp token and Phone number ID
   into `.env`, set `VERIFY_TOKEN` to any string.
2. Expose the port: `ngrok http 8000`.
3. Configuration → Callback URL `https://<ngrok>/webhook`, verify token as
   above, subscribe to **messages**.
4. Add your own number as a test recipient, then put it in
   `fleet_managers.csv` (digits only, with country code, no `+`).

Set `APP_SECRET` once it works and the webhook starts rejecting unsigned
requests.

## Going to production

- **`call_vehicle_api` in helpers.py** is the only function to rewrite. It
  returns `{ok, status, latency_ms, error}`; keep that shape and nothing else
  changes.
- **Swap the CSVs for Postgres** by rewriting `load_data`, `get_fleet_manager`,
  `get_owned_vehicle`, `log_audit`. Nothing in `main.py` touches storage.
- **Sessions and dedupe are in-process.** Run one worker, or move both dicts to
  Redis before scaling out.

## Design notes

**Dedupe is load-bearing.** Meta retries the webhook when a response is slow.
Without the `wamid` check, one sluggish vehicle call becomes three panic
alarms. That's why `/webhook` returns 200 immediately and does the work in a
background task.

**Errors are deliberately vague.** "Not linked to your account" covers wrong
fleet, inactive, and nonexistent alike — anything more specific lets a stranger
enumerate your fleet one plate at a time. The audit log records the real reason
(`DENIED_OTHER_FLEET` vs `DENIED_NO_SUCH_VEHICLE`) for support.

**Buttons carry their own context.** Payloads look like `ACT|UNLOCK|HRX1234`,
so a restart mid-conversation doesn't strand anyone. The session only enforces
expiry — 5 min to pick an action, 2 min to confirm — so nobody taps yesterday's
button and unlocks a truck.

**Ownership is re-checked on every tap**, not just when the plate is typed. A
button payload is user-supplied input and can be replayed.

**Audit writes at every stage**, not just on success. Four rows for one unlock:
REQUEST, ACTION_SELECTED, CONFIRMED, EXECUTED. When someone asks who unlocked
HRX1234 at 2am you need the attempt even if the API failed. The `Ref: REQ-XXXX`
in the reply is the join key back to those rows.

**Panic never auto-retries** and gets explicit consequence text ("horn and
lights, 30 seconds, driver alerted"). Lock and unlock are near-idempotent;
panic isn't.

**Confirm step is configurable.** `REQUIRE_CONFIRM` in `helpers.py` — set
`UNLOCK` and `LOCK` to `False` for one-tap if two taps is too slow for a
manager doing this forty times a day. Keep `PANIC` on.

## Approvals and the roster portal

Who's who (see `whatsapp-vehicle-bot_instructions.md`): **FleetManagers**
request actions; **ControlRoomAdmins** (12 shift staff) approve them;
**EngineeringTeam_<ROLE>** (CTO, CEO, PM, HOS…) run the roster portal and are
the fallback approvers.

Confirming an action no longer moves the vehicle. It creates a pending request
(`Ref: REQ-XXXXXX`) and sends Approve / Reject buttons to every active
ControlRoomAdmin on the current shift in `data/control_room_admins.csv`
(3 × 8h shifts in `TIMEZONE`).

- The first decision wins. Later taps are told who already decided, and the
  vehicle command runs exactly once.
- Buttons carry only the Ref. The vehicle, action and allowed approvers live
  server-side, so a payload can't be edited to approve something else.
- No answer within `APPROVAL_TTL` (default 10 min) → expired, everyone told.
- Nobody on duty → the request escalates to the `ENGINEERING_TEAM_<ROLE>` numbers.
- Ownership is re-checked at approval time, not just at request time.
- Pending requests are in memory: a restart drops them and the FleetManager
  has to ask again.

`/elockadmins` is the roster page: WhatsApp-OTP sign-in for the
`ENGINEERING_TEAM_<ROLE>` numbers in `.env` only; drag ControlRoomAdmins
between shifts or onto the bench.
Saves are validated, written atomically to `control_room_admins.csv`, take effect
immediately, and are audit-logged with who changed what. `/audit` needs a
portal sign-in.

## Known gaps for v2

- **Approver and OTP messages are business-initiated.** WhatsApp delivers
  free-form messages only to people who messaged the bot in the last 24h, so
  an approver who hasn't will silently miss requests (look for
  `DELIVERY FAILED ... 131047` in the log). Fix: approved utility template for
  approval requests and an authentication template for the OTP. Until then,
  approvers should message the bot at the start of each shift.

- 24-hour window: free-form replies only work within 24h of the manager's last
  message. Fine here since they always message first, but a proactive
  notification would need an approved template.
- No rate limiting per manager.
- `load_data()` is called on `/health`, so it doubles as a config reload.