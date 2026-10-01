"""
test_webhook.py — exercise the bot without WhatsApp.

    python test_webhook.py                          # local, via /simulate
    python test_webhook.py --url https://xxx.ngrok-free.dev   # through the tunnel

Edit PHONE to a number that exists in data/fleet_managers.csv.
Steps 4-5 use /simulate, which is only enabled with ENABLE_SIMULATE=yes
(the default only when WhatsApp credentials are blank).
"""

import argparse
import hashlib
import hmac
import json
import os
import random
import sys

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PHONE = "918882374849"   # must match a row in fleet_managers.csv
VEHICLE = "HRX1001"      # must belong to that manager

APP_SECRET = os.getenv("APP_SECRET", "").strip()


def signed_post(client: httpx.Client, url: str, payload: dict) -> httpx.Response:
    """
    POST the way Meta does. When APP_SECRET is set the webhook rejects
    anything without a matching X-Hub-Signature-256, so sign it here too.
    """
    raw = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json"}
    if APP_SECRET:
        digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={digest}"
    return client.post(url, content=raw, headers=headers)


def safe_json(r: httpx.Response) -> dict:
    """The server can drop a connection mid-request under --reload."""
    try:
        return r.json()
    except Exception:
        print(f"      (no JSON back — status {r.status_code}. "
              "Running with --reload? Restart uvicorn without it.)")
        return {}


def meta_payload(phone: str, text: str = "", button: str = "") -> dict:
    """The exact shape Meta posts to /webhook."""
    msg = {
        "id": f"wamid.TEST{random.randint(10**9, 10**10)}",  # unique or dedupe eats it
        "from": phone,
        "timestamp": "1700000000",
        "type": "interactive" if button else "text",
    }
    if button:
        msg["interactive"] = {
            "type": "button_reply",
            "button_reply": {"id": button, "title": "tap"},
        }
    else:
        msg["text"] = {"body": text}

    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "0",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "15551381227",
                                 "phone_number_id": "0"},
                    "messages": [msg],
                },
            }],
        }],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000",
                    help="base URL (ngrok URL to test the full path)")
    ap.add_argument("--token", default="my-verify-token-123")
    ap.add_argument("--phone", default=PHONE)
    ap.add_argument("--vehicle", default=VEHICLE)
    args = ap.parse_args()
    base = args.url.rstrip("/")

    print(f"\ntarget: {base}\nphone:  {args.phone}\nvehicle: {args.vehicle}")
    print("signing:", "yes (APP_SECRET set)" if APP_SECRET else "no (APP_SECRET blank)")
    client = httpx.Client(timeout=20, follow_redirects=True)

    # 1 — is it up?
    print("\n[1] GET /health")
    try:
        r = client.get(f"{base}/health")
        print("   ", r.status_code, r.text[:120])
        if r.status_code != 200:
            print("    ✗ server not reachable — stopping")
            return 1
    except Exception as exc:
        print(f"    ✗ {exc}")
        return 1

    # 2 — the handshake Meta performs
    print("\n[2] GET /webhook  (verify handshake)")
    r = client.get(f"{base}/webhook", params={
        "hub.mode": "subscribe",
        "hub.verify_token": args.token,
        "hub.challenge": "12345",
    })
    ok = r.text.strip() == "12345"
    print("   ", r.status_code, r.text[:60], "✓" if ok else "✗ VERIFY_TOKEN mismatch")

    # 3 — full conversation as Meta would deliver it
    steps = [
        ("send vehicle number", {"text": args.vehicle}),
        ("tap Unlock",          {"button": f"ACT|UNLOCK|{args.vehicle}"}),
        ("tap Yes, unlock",     {"button": f"CONF|UNLOCK|{args.vehicle}"}),
    ]
    print("\n[3] POST /webhook  (real Meta payload shape)")
    for label, kw in steps:
        r = signed_post(client, f"{base}/webhook", meta_payload(args.phone, **kw))
        note = ""
        if r.status_code == 403:
            note = "  <- signature rejected; is APP_SECRET in .env the same?"
        print(f"    {label:<22} -> {r.status_code} {r.text[:40]}{note}")
    print("    ↑ replies go to WhatsApp (or the console in DRY_RUN).")
    print("      Watch the uvicorn window for [audit] lines.")

    # 4 — same flow through /simulate, which hands the replies back
    print("\n[4] POST /simulate  (bot replies returned inline)")
    for label, kw in steps:
        body = {"phone": args.phone, "wamid": f"sim{random.randint(1, 10**9)}", **kw}
        r = client.post(f"{base}/simulate", json=body)
        print(f"\n    --- {label} ---")
        for m in safe_json(r).get("replies", []):
            for line in m["body"].splitlines():
                print("     ", line)
            if m["kind"] == "buttons":
                print("      " + "  ".join(f"[{t}]" for _, t in m["buttons"]))

    # 5 — the denial paths
    print("\n[5] denial paths")
    for label, phone, text in [
        ("unknown sender ", "919000000000", args.vehicle),
        ("other's vehicle", args.phone, "HRX3001"),
        ("no such vehicle", args.phone, "HRX9999"),
    ]:
        r = client.post(f"{base}/simulate", json={
            "phone": phone, "text": text,
            "wamid": f"sim{random.randint(1, 10**9)}"})
        reply = safe_json(r).get("replies", [{}])
        first = reply[0].get("body", "(nothing)").splitlines()[0] if reply else "(nothing)"
        print(f"    {label} -> {first}")

    # 6 — dedupe
    print("\n[6] dedupe (same wamid twice)")
    dup = meta_payload(args.phone, text=args.vehicle)
    signed_post(client, f"{base}/webhook", dup)
    signed_post(client, f"{base}/webhook", dup)
    print("    sent twice — uvicorn should log '[dedupe] ignoring repeat' once")

    print("\ndone. Check data/audit_log.csv for the trail.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())