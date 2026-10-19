# coding: utf-8
"""
moomoo_config.py — Central configuration for dual OpenD instances.

Two Moomoo accounts are supported simultaneously via separate OpenD processes:
  • AU account (SecurityFirm.FUTUAU)  →  OpenD on port 11111
  • SG account (SecurityFirm.FUTUSG)  →  OpenD on port 11112

Start both OpenD processes via:
    powershell start_opend.ps1

Each OpenD process loads its own config:
    opend_configs/au/OpenD.xml   (port 11111, login_region=AU)
    opend_configs/sg/OpenD.xml   (port 11112, login_region=SG)
"""

import os
import socket

# Must be set before importing moomoo
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

from moomoo import SecurityFirm, TrdEnv, TrdMarket

# Suppress Moomoo SDK internal connect/disconnect logs (open_context_base.py)
import logging as _logging
_logging.getLogger("FTFileLog").setLevel(_logging.CRITICAL)
_logging.getLogger("FTConsoleLog").setLevel(_logging.CRITICAL)

# ── Host ──────────────────────────────────────────────────────────────────────
OPEND_HOST = "127.0.0.1"

# ── Per-account ports ─────────────────────────────────────────────────────────
OPEND_AU_PORT = 11111   # AU account  (SecurityFirm.FUTUAU)
OPEND_SG_PORT = 11112   # SG account  (SecurityFirm.FUTUSG)

# Backward-compat alias (scripts that only care about one port use this)
OPEND_DEFAULT_PORT = OPEND_AU_PORT

# ── Per-account identity ──────────────────────────────────────────────────────
AU_FIRM       = SecurityFirm.FUTUAU
SG_FIRM       = SecurityFirm.FUTUSG
AU_TRD_ENV    = TrdEnv.REAL
SG_TRD_ENV    = TrdEnv.REAL
AU_ACCOUNT_ID = 284008279318020118   # FUTUAU REAL trading acc_id (from get_acc_list — login: +61 470625861 phone)
SG_ACCOUNT_ID = 283726805871679562   # FUTUSG REAL trading acc_id (NOT login user ID 151763892)

# All supported accounts: (label, port, SecurityFirm, account_id)
ACCOUNTS = [
    ("AU", OPEND_AU_PORT, AU_FIRM, AU_ACCOUNT_ID),
    ("SG", OPEND_SG_PORT, SG_FIRM, SG_ACCOUNT_ID),
]

# ── Connectivity helpers ──────────────────────────────────────────────────────

def is_opend_running(port: int, host: str = OPEND_HOST, timeout: float = 2.0) -> bool:
    """Return True if OpenD is listening on *host:port*."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def active_ports() -> list[int]:
    """Return ports where OpenD is currently reachable."""
    return [port for _, port, _, _ in ACCOUNTS if is_opend_running(port)]


def active_accounts() -> list[tuple]:
    """Return (label, port, firm, acc_id) tuples for running OpenD instances."""
    return [(lbl, port, firm, acc) for lbl, port, firm, acc in ACCOUNTS
            if is_opend_running(port)]


def quote_port() -> int:
    """
    Return the port of any running OpenD for quote-only usage.
    Prefers AU; falls back to SG; raises RuntimeError if neither is up.
    """
    for port in (OPEND_AU_PORT, OPEND_SG_PORT):
        if is_opend_running(port):
            return port
    raise RuntimeError(
        "No OpenD instance reachable on ports "
        f"{OPEND_AU_PORT} (AU) or {OPEND_SG_PORT} (SG). "
        "Start OpenD via start_opend.ps1."
    )


def ensure_opend_running(timeout: int = 30) -> bool:
    """
    Start OpenD via start_opend.ps1 if neither port is reachable.
    Launches in a new console window so the user can handle any captcha prompt.
    Polls every second for up to *timeout* seconds.
    Returns True if OpenD becomes reachable within the timeout, False otherwise.
    """
    import subprocess
    import time as _time
    from pathlib import Path as _Path

    # Already running — nothing to do
    if is_opend_running(OPEND_AU_PORT) or is_opend_running(OPEND_SG_PORT):
        return True

    script = _Path(__file__).parent / "start_opend.ps1"
    if not script.exists():
        print(f"[moomoo] start_opend.ps1 not found at {script}")
        return False

    print(f"[moomoo] OpenD not running — launching {script.name} in a new window ...")
    try:
        _flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)  # Windows only
        subprocess.Popen(
            ["powershell", "-ExecutionPolicy", "RemoteSigned", "-File", str(script)],
            creationflags=_flags,
        )
    except Exception as exc:
        print(f"[moomoo] Failed to launch {script.name}: {exc}")
        return False

    # Poll until OpenD responds or timeout
    for i in range(timeout):
        _time.sleep(1)
        if is_opend_running(OPEND_AU_PORT) or is_opend_running(OPEND_SG_PORT):
            print(f"[moomoo] OpenD started successfully ({i + 1}s)")
            return True

    print(
        f"[moomoo] OpenD did not respond within {timeout}s. "
        "If a captcha prompt appeared, complete it in the new window then retry."
    )
    return False


def api_heartbeat(
    port: int | None = None,
    timeout_s: float = 5.0,
    test_symbol: str = "US.AAPL",
) -> tuple[bool, str]:
    """
    Test actual Moomoo API responsiveness beyond just TCP connectivity.

    ``is_opend_running()`` only checks if the TCP port accepts connections.
    ``api_heartbeat()`` calls ``get_market_snapshot()`` to verify the SDK
    can actually communicate with OpenD — catching the case where the TCP
    port is open but the SDK is internally stuck (stale connection).

    Prefer AU port 11111 (300 subscription quota) over SG 11112 (100 quota).

    Returns
    -------
    (True,  "ok:{port}")                   Moomoo API responded OK
    (False, "tcp_down:{port}")             TCP not reachable
    (False, "timeout:{port}:{timeout_s}s") API did not respond in time
    (False, "error:{port}:snapshot_failed") API call returned error/empty
    """
    import concurrent.futures as _cf

    # Prefer AU (300 subscription quota) over SG (100 quota)
    if port is None:
        port = OPEND_AU_PORT if is_opend_running(OPEND_AU_PORT) else OPEND_SG_PORT

    if not is_opend_running(port):
        return False, f"tcp_down:{port}"

    def _probe() -> bool:
        from moomoo import OpenQuoteContext, RET_OK  # noqa: PLC0415
        ctx = OpenQuoteContext(host=OPEND_HOST, port=port)
        try:
            ret, data = ctx.get_market_snapshot([test_symbol])
            return ret == RET_OK and data is not None and not data.empty
        except Exception:
            return False
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    # Golden Rule #16: never use `with ThreadPoolExecutor()` when also
    # calling fut.result(timeout=N) — use shutdown(wait=False) instead.
    _ex = _cf.ThreadPoolExecutor(max_workers=1)
    _fut = _ex.submit(_probe)
    try:
        ok = _fut.result(timeout=timeout_s)
        if ok:
            return True, f"ok:{port}"
        return False, f"error:{port}:snapshot_failed"
    except _cf.TimeoutError:
        return False, f"timeout:{port}:{timeout_s}s"
    finally:
        _ex.shutdown(wait=False)
