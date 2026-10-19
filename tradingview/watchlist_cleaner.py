"""
WatchlistCleaner — Auto-remove non-tradable symbols from TradingView watchlists
================================================================================
Consumes the ``skipped_non_tradable`` list produced by TradabilityFilter and
removes flagged symbols from TradingView via the unofficial Watchlistify API.

Usage (from BatchAnalyzer / WebhookServer):
    from watchlist_cleaner import WatchlistCleaner
    WatchlistCleaner().process_skipped(skipped_non_tradable)

Standalone:
    python watchlist_cleaner.py --symbol TYO:7203 --watchlist ALL_NEW
"""

from __future__ import annotations

import json
import logging
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

# ── Windows: force UTF-8 console ──────────────────────────────────────────────
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger(__name__)

# ── Path setup ────────────────────────────────────────────────────────────────
_THIS     = Path(__file__).parent
_ROOT     = _THIS.parent
_WM_DIR   = _THIS / "watchlist-management"

COOKIE_FILE = _THIS / "cookie"

# ── Watchlistify API base ─────────────────────────────────────────────────────
_WATCHLIST_API_BASE = "https://watchlistify.tradingview.com/v1/user/watchlists"

# ── Sources that originate from the watchlist (eligible for auto-clean) ───────
_WATCHLIST_SOURCES: frozenset[str] = frozenset({
    "watchlist", "download", "batch",
})


# ─────────────────────────────────────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────────────────────────────────────

def _read_cookie(cookie_file: Path = COOKIE_FILE) -> tuple[str, str]:
    """
    Parse ``sessionid`` and ``sessionid_sign`` from the TV cookie file.
    Returns (sessionid, sessionid_sign) — both empty strings on failure.
    """
    if not cookie_file.exists():
        return "", ""
    raw = cookie_file.read_text(encoding="utf-8").strip()
    sid = sid_sign = ""
    for part in raw.split(";"):
        p = part.strip()
        if p.startswith("sessionid="):
            sid = p[len("sessionid="):]
        elif p.startswith("sessionid_sign="):
            sid_sign = p[len("sessionid_sign="):]
    return sid, sid_sign


def _make_headers(sessionid: str, sessionid_sign: str) -> dict:
    cookie_str = f"sessionid={sessionid}; sessionid_sign={sessionid_sign}"
    return {
        "Cookie":       cookie_str,
        "User-Agent":   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 AppleWebKit/537.36",
        "Origin":       "https://www.tradingview.com",
        "Referer":      "https://www.tradingview.com/",
        "Accept":       "application/json",
        "Content-Type": "application/json",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Low-level TV Watchlist API
# ─────────────────────────────────────────────────────────────────────────────

def _tv_get(url: str, headers: dict) -> dict | list | None:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        logger.warning("TV API GET %s → HTTP %s", url, exc.code)
        return None
    except Exception as exc:
        logger.warning("TV API GET %s → %s", url, exc)
        return None


def _tv_put(url: str, payload: dict, headers: dict) -> dict | None:
    body = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(url, data=body, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_bytes = b""
        try:
            body_bytes = exc.read()
        except Exception:
            pass
        logger.warning("TV API PUT %s → HTTP %s  body=%s", url, exc.code, body_bytes[:200])
        return None
    except Exception as exc:
        logger.warning("TV API PUT %s → %s", url, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# WatchlistCleaner
# ─────────────────────────────────────────────────────────────────────────────

class WatchlistCleaner:
    """
    Remove non-tradable symbols from TradingView watchlists via the Watchlistify API.

    Parameters
    ----------
    cookie_file : Path, optional
        Path to TV session cookie file. Defaults to tradingview/cookie.
    default_watchlist : str
        Watchlist name to clean (default ``"ALL_NEW"``).
    dry_run : bool
        If True, compute the change but do not push to TV.
    """

    def __init__(
        self,
        cookie_file: Path | None = None,
        default_watchlist: str = "ALL_NEW",
        dry_run: bool = False,
    ) -> None:
        self._cookie_file       = cookie_file or COOKIE_FILE
        self.default_watchlist  = default_watchlist
        self.dry_run            = dry_run

        # Cache auth per instance (refreshed when None)
        self._sessionid:      str | None = None
        self._sessionid_sign: str | None = None
        self._watchlist_id_cache: dict[str, str] = {}

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _ensure_auth(self) -> bool:
        if self._sessionid and self._sessionid_sign:
            return True
        sid, sid_sign = _read_cookie(self._cookie_file)
        if not sid:
            logger.warning("WatchlistCleaner: no TV session cookie found at %s", self._cookie_file)
            return False
        self._sessionid      = sid
        self._sessionid_sign = sid_sign
        return True

    def _headers(self) -> dict:
        return _make_headers(self._sessionid or "", self._sessionid_sign or "")

    # ── Watchlist discovery ───────────────────────────────────────────────────

    def _get_watchlist_id(self, watchlist_name: str) -> str | None:
        """Fetch the numeric ID for a named watchlist from the TV API."""
        if watchlist_name in self._watchlist_id_cache:
            return self._watchlist_id_cache[watchlist_name]

        data = _tv_get(_WATCHLIST_API_BASE, self._headers())
        if not isinstance(data, (list, dict)):
            logger.warning("WatchlistCleaner: could not fetch watchlists list")
            return None

        items = data if isinstance(data, list) else data.get("watchlists", data.get("data", []))
        for item in items:
            name = item.get("name", "")
            wid  = str(item.get("id", ""))
            if name == watchlist_name and wid:
                self._watchlist_id_cache[watchlist_name] = wid
                logger.debug("WatchlistCleaner: %s → id=%s", watchlist_name, wid)
                return wid

        logger.warning(
            "WatchlistCleaner: watchlist %r not found; available: %s",
            watchlist_name,
            [i.get("name") for i in items[:10]],
        )
        return None

    def _get_symbols(self, watchlist_id: str) -> list[str] | None:
        """Fetch the full symbol list for a watchlist."""
        url  = f"{_WATCHLIST_API_BASE}/{watchlist_id}/symbols"
        data = _tv_get(url, self._headers())
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("symbols", data.get("data", None))
        return None

    def _push_symbols(self, watchlist_id: str, symbols: list[str]) -> bool:
        """Push updated symbol list back via PUT."""
        url    = f"{_WATCHLIST_API_BASE}/{watchlist_id}/symbols"
        result = _tv_put(url, {"symbols": symbols}, self._headers())
        return result is not None

    # ── Public API ────────────────────────────────────────────────────────────

    def remove_from_watchlist(
        self,
        tv_symbol: str,
        watchlist_name: str | None = None,
    ) -> bool:
        """
        Remove ``tv_symbol`` from the named watchlist.

        Returns True on success, False on any failure (no exception raised).
        """
        wl = watchlist_name or self.default_watchlist

        if not self._ensure_auth():
            return False

        wid = self._get_watchlist_id(wl)
        if not wid:
            return False

        symbols = self._get_symbols(wid)
        if symbols is None:
            logger.warning("WatchlistCleaner: could not fetch symbols for watchlist %s", wl)
            return False

        if tv_symbol not in symbols:
            logger.debug("WatchlistCleaner: %s not in %s (nothing to remove)", tv_symbol, wl)
            return True   # idempotent success

        updated = [s for s in symbols if s != tv_symbol]
        logger.info(
            "WatchlistCleaner: removing %s from %s (%d → %d symbols)",
            tv_symbol, wl, len(symbols), len(updated),
        )

        if self.dry_run:
            logger.info("DRY RUN — would remove %s from %s", tv_symbol, wl)
            return True

        ok = self._push_symbols(wid, updated)
        if ok:
            print(f"  CLEANED {tv_symbol} removed from watchlist {wl!r}")
        else:
            logger.warning("WatchlistCleaner: push failed for %s in %s", tv_symbol, wl)
        return ok

    def process_skipped(
        self,
        skipped: list[dict],
        watchlist_name: str | None = None,
    ) -> dict:
        """
        Process a list of skipped non-tradable signals.

        Only acts on symbols whose ``source`` is in ``_WATCHLIST_SOURCES``.
        Forex and Crypto symbols are **never** auto-removed.

        Parameters
        ----------
        skipped : list[dict]
            Each dict: {symbol, reason, source, [result]}

        Returns
        -------
        dict with keys ``cleaned`` and ``manual_required``
        """
        if not skipped:
            return {"cleaned": [], "manual_required": []}

        # Lazily import to avoid circular dependency
        try:
            sys.path.insert(0, str(_THIS))
            from tradability_filter import TradabilityFilter
            _tf = TradabilityFilter()
        except ImportError:
            _tf = None

        cleaned:  list[dict] = []
        manual_required: list[dict] = []

        for item in skipped:
            symbol = item.get("symbol", "")
            reason = item.get("reason", "")
            source = item.get("source", "")

            if not symbol:
                continue

            # Skip FX and Crypto — never auto-remove from watchlist
            if _tf and _tf.is_forex_or_crypto(symbol):
                logger.debug(
                    "WatchlistCleaner: skipping %s (FX/Crypto — never auto-remove)", symbol
                )
                continue

            # Only clean symbols that originated from the watchlist
            if source not in _WATCHLIST_SOURCES:
                logger.debug(
                    "WatchlistCleaner: skipping %s (source=%r — not a watchlist source)", symbol, source
                )
                continue

            ok = self.remove_from_watchlist(symbol, watchlist_name)
            if ok:
                cleaned.append({"symbol": symbol, "reason": reason})
            else:
                logger.warning(
                    "WATCHLIST_CLEANUP_REQUIRED: unable to remove %s — %s", symbol, reason
                )
                manual_required.append({"symbol": symbol, "reason": reason})

        if manual_required:
            self._print_manual_required(manual_required)

        return {"cleaned": cleaned, "manual_required": manual_required}

    # ── Notification ──────────────────────────────────────────────────────────

    @staticmethod
    def _print_manual_required(items: list[dict]) -> None:
        width = 54
        border  = "═" * width
        divider = "╠" + "═" * width + "╣"
        print(f"\n╔{border}╗")
        print(f"║{'  WATCHLIST CLEANUP REQUIRED (manual action)':^{width}}║")
        print(divider)
        print(f"║{'  Remove these tickers from TradingView manually:':^{width}}║")
        for item in items:
            sym    = item.get("symbol", "")
            reason = item.get("reason", "")[:35]
            line   = f"    - {sym}  (reason: {reason})"
            print(f"║{line:<{width}}║")
        print(f"╚{border}╝\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Remove non-tradable symbols from TradingView watchlists"
    )
    parser.add_argument("--symbol",     help="TV symbol to remove (e.g. TYO:7203)")
    parser.add_argument("--watchlist",  default="ALL_NEW", help="Watchlist name (default ALL_NEW)")
    parser.add_argument("--dry-run",    action="store_true", help="Print what would happen, don't push")
    parser.add_argument("--verbose",    action="store_true", help="Enable DEBUG logging")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cleaner = WatchlistCleaner(dry_run=args.dry_run)

    if args.symbol:
        ok = cleaner.remove_from_watchlist(args.symbol, args.watchlist)
        print("✅ Done" if ok else "❌ Failed")
    else:
        parser.print_help()


if __name__ == "__main__":
    _main()
