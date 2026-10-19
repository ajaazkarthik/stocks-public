# coding: utf-8
"""
india_brokers.py — Common entry point for Indian broker integrations
Analogous to moomoo_test.py but for Zerodha + Angel One.

Usage:
  python india_brokers.py              # runs both brokers
  python india_brokers.py zerodha      # Zerodha only
  python india_brokers.py angelone     # Angel One only

ENV VARS required (see each broker's test script for details):

  Zerodha (Kite Connect):
    ZERODHA_API_KEY         — Kite Connect app api_key
    ZERODHA_API_SECRET      — api_secret (only needed when generating new session)
    ZERODHA_ACCESS_TOKEN    — cached access_token (skip browser login if set)

  Angel One (SmartAPI):
    ANGELONE_API_KEY        — SmartAPI app api_key
    ANGELONE_CLIENT_CODE    — Angel One client code / user ID
    ANGELONE_PIN            — 4-digit trading PIN
    ANGELONE_TOTP_SECRET    — Base32 TOTP secret from Angel One TOTP setup
"""

import sys
import os
import subprocess

# Fix Windows console encoding
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass
    try: sys.stderr.reconfigure(encoding="utf-8")
    except Exception: pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

BROKERS = {
    "zerodha":  os.path.join(SCRIPT_DIR, "zerodha_test.py"),
    "angelone": os.path.join(SCRIPT_DIR, "angelone_test.py"),
}


def _has_env(keys):
    """Return True if ALL listed env vars are set."""
    return all(os.environ.get(k, "").strip() for k in keys)


def run_broker(name: str, script: str):
    """Run a broker test script as a subprocess (inherits env)."""
    header = f"  RUNNING: {name.upper()}"
    print()
    print("=" * 65)
    print(header)
    print("=" * 65)

    if not os.path.isfile(script):
        print(f"[SKIP] Script not found: {script}")
        return

    result = subprocess.run(
        [sys.executable, script],
        env=os.environ.copy(),
    )
    if result.returncode != 0:
        print(f"\n[WARN] {name} script exited with code {result.returncode}")


if __name__ == "__main__":
    # Determine which brokers to run
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else "all"

    if arg in ("zerodha", "z"):
        targets = ["zerodha"]
    elif arg in ("angelone", "angel", "ao", "a"):
        targets = ["angelone"]
    else:
        targets = list(BROKERS.keys())

    # Credential check — warn if any env vars appear missing
    checks = {
        "zerodha":  ["ZERODHA_API_KEY"],
        "angelone": ["ANGELONE_API_KEY", "ANGELONE_CLIENT_CODE",
                     "ANGELONE_PIN", "ANGELONE_TOTP_SECRET"],
    }

    print("=" * 65)
    print("  India Brokers — Credential Check")
    print("=" * 65)
    for broker in targets:
        keys = checks[broker]
        ready = _has_env(keys)
        status = "READY" if ready else "MISSING CREDS"
        print(f"  {broker:<12} [{status}]")
        if not ready:
            missing = [k for k in keys if not os.environ.get(k, "").strip()]
            for m in missing:
                print(f"    - {m}")

    # Run each broker
    for broker in targets:
        run_broker(broker, BROKERS[broker])

    print()
    print("=" * 65)
    print("  All done.")
    print("=" * 65)
