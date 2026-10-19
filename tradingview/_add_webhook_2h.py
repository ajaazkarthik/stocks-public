"""
Add webhook URL to WIN 2H alert (2381963449).

The WIN 1H alert already has a webhook configured via TV UI.
This script fetches the live alert state, reads the 1H webhook URL,
then patches it onto the 2H alert using modify_restart_alert.

Usage:
    python _add_webhook_2h.py
    python _add_webhook_2h.py --webhook https://your-custom-url.ngrok.io/webhook
"""
import json
import argparse
import requests
from pathlib import Path

BASE = Path(__file__).parent
cookie_raw = BASE.joinpath("cookie").read_text().strip()
cookies = {}
for field in cookie_raw.split(";"):
    k, _, v = field.strip().partition("=")
    if k.strip():
        cookies[k.strip()] = v.strip()

UID       = 57923804
USERNAME  = "ajaazkarthik1"
BASE_URL  = "https://pricealerts.tradingview.com"
PARAMS    = {
    "log_username": USERNAME,
    "maintenance_unset_reason": "initial_operated",
    "build_time": "2026-04-06T00:00:00",
}
HDRS = {
    "accept": "*/*",
    "content-type": "text/plain;charset=UTF-8",
    "origin": "https://www.tradingview.com",
    "referer": "https://www.tradingview.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    ),
}

WIN_1H_ID = 2381961922   # has webhook set
WIN_2H_ID = 2381963449   # missing webhook


def post(endpoint, data):
    body = json.dumps({"payload": data})
    r = requests.post(
        f"{BASE_URL}/{endpoint}",
        data=body.encode(),
        headers=HDRS,
        params=PARAMS,
        cookies=cookies,
        timeout=15,
    )
    return r.json()


def get_alerts():
    r = requests.get(
        f"{BASE_URL}/list_alerts",
        headers=HDRS,
        params={**PARAMS, "user_id": UID},
        cookies=cookies,
        timeout=15,
    )
    resp = r.json()
    if resp.get("s") != "ok":
        raise RuntimeError(f"list_alerts failed: {resp}")
    return resp.get("r", [])


def main():
    parser = argparse.ArgumentParser(description="Add webhook to WIN 2H alert")
    parser.add_argument("--webhook", default=None,
                        help="Override webhook URL (auto-detects from 1H alert by default)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be sent without making the API call")
    args = parser.parse_args()

    print("Fetching live alerts...")
    alerts = get_alerts()
    print(f"  Found {len(alerts)} alert(s)")

    alert_1h = next((a for a in alerts if a.get("alert_id") == WIN_1H_ID), None)
    alert_2h = next((a for a in alerts if a.get("alert_id") == WIN_2H_ID), None)

    if not alert_1h:
        print(f"ERROR: WIN 1H alert ({WIN_1H_ID}) not found — cannot auto-detect webhook URL")
        if not args.webhook:
            print("  Pass --webhook <url> explicitly.")
            return
    if not alert_2h:
        print(f"ERROR: WIN 2H alert ({WIN_2H_ID}) not found")
        return

    # Determine webhook URL
    if args.webhook:
        webhook_url = args.webhook
        print(f"  Using provided webhook: {webhook_url}")
    else:
        webhook_url = alert_1h.get("web_hook") or ""
        if not webhook_url:
            print(
                "WARNING: 1H alert also has no webhook_url set in API response.\n"
                "  The webhook may have been set via TV UI but not yet reflected in this snapshot.\n"
                "  Pass --webhook <your-ngrok-or-cf-tunnel-url>/webhook explicitly."
            )
            return
        print(f"  Auto-detected webhook from 1H alert: {webhook_url}")

    # Determine message template (copy from 1H, or keep existing 2H)
    msg_template = (alert_1h or {}).get("message") or alert_2h.get("message") or ""

    print(f"\nCurrent 2H webhook : {alert_2h.get('web_hook')!r}")
    print(f"Target  2H webhook : {webhook_url!r}")
    print(f"Message template   : {msg_template!r}")

    if alert_2h.get("web_hook") == webhook_url:
        print("\nNothing to do — 2H alert already has the same webhook URL.")
        return

    # Build modify payload (send original data + updated webhook)
    skip_keys = {
        "active", "create_time", "last_fire_time", "last_fire_bar_time",
        "last_error", "last_stop_reason", "complexity",
        "presentation_data", "symbolset_data", "pro_symbol",
    }
    payload = {k: v for k, v in alert_2h.items() if k not in skip_keys}
    payload["uid"]      = UID
    payload["web_hook"] = webhook_url
    if msg_template:
        payload["message"] = msg_template

    print(f"\nPayload keys: {sorted(payload.keys())}")

    if args.dry_run:
        print("\n[DRY RUN] Would POST modify_restart_alert with:")
        print(json.dumps(payload, indent=2, default=str))
        return

    print("\nCalling modify_restart_alert...")
    result = post("modify_restart_alert", payload)
    print(f"  Response: s={result.get('s')!r}  err={result.get('err')}")

    if result.get("s") == "ok":
        print("\nSUCCESS: WIN 2H alert webhook updated!")
        print(f"  Alert {WIN_2H_ID} (2H) now points to: {webhook_url}")
        print("  Both WIN alerts will now send to the same webhook server.")
    else:
        errmsg = result.get("errmsg") or result.get("err", {}).get("code", "unknown")
        print(f"\nFAILED: {errmsg}")
        if "invalid_request" in str(result):
            print(
                "\n  HINT: 'invalid_request' can mean the Pro plan features are needed for\n"
                "  strategy alert modification. If the plan is active, ensure the cookie\n"
                "  in tradingview/cookie is fresh (re-export from browser).\n"
                "  Alternatively, open TradingView → Alerts → WIN (2H) → Edit → add webhook manually."
            )


if __name__ == "__main__":
    main()
