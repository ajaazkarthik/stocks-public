"""Stable delivery identities for scheduler notifications.

A delivery identity answers one question: *has this exact piece of information
already been delivered to this recipient?* It deliberately does **not** include
the analysis run ID, because a restart or a re-scoring run must not resend a
fire that was already delivered.

Stock identity  = recipient + notification kind + ``(pro_symbol, fire_time,
resolution)`` — the same natural fire key used by ``database/fires_ledger.py``.

Option identity = recipient + notification kind + the actual contract
(exchange/symbol/expiry/strike/type) plus the originating signal/update policy,
so a different contract on the same ticker is a different notification.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

SCHEMA_VERSION = 1

KIND_STOCK = "stock"
KIND_OPTION = "option"
KIND_SUMMARY = "summary"

#: Notification kinds that carry a per-fire identity and are therefore
#: deduplicated by fire rather than by cycle.
FIRE_KINDS = frozenset({KIND_STOCK, KIND_OPTION})


def _norm(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _norm_upper(value: Any) -> str:
    return _norm(value).upper()


def _norm_number(value: Any) -> str:
    """Normalize a numeric field so ``530`` and ``530.0`` collapse."""
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _norm(value)
    if number == int(number):
        return str(int(number))
    return repr(number)


def stock_fire_key(stock: dict[str, Any]) -> tuple[str, str, str]:
    """Return the natural fire identity ``(pro_symbol, fire_time, resolution)``.

    ``pro_symbol`` is preferred over ``symbol`` so the identity matches the
    ledger's own key even when only the exchange-qualified symbol is present.
    """
    pro = _norm_upper(stock.get("pro_symbol") or stock.get("tv_symbol"))
    if not pro:
        exchange = _norm_upper(stock.get("exchange"))
        symbol = _norm_upper(stock.get("symbol"))
        pro = f"{exchange}:{symbol}" if exchange and symbol else symbol
    return pro, _norm(stock.get("fire_time")), _norm(stock.get("resolution"))


def option_contract_key(option: dict[str, Any]) -> tuple[str, ...]:
    """Return the normalized contract identity for one option row.

    A validated ``contract_id``/``option_symbol``/``occ_symbol`` wins when
    present; otherwise the exchange/symbol/expiry/strike/type tuple is used.
    """
    for field in ("contract_id", "option_symbol", "occ_symbol", "contract_symbol"):
        value = _norm(option.get(field))
        if value:
            return ("id", value)
    return (
        "key",
        _norm_upper(option.get("exchange")),
        _norm_upper(option.get("symbol")),
        _norm(option.get("expiry")),
        _norm_number(option.get("strike")),
        _norm_upper(option.get("opt_type") or option.get("option_type")),
    )


def option_policy_key(option: dict[str, Any]) -> str:
    """Return the originating signal/update policy for an option row.

    ``strategy_modes`` and the parent signal score describe *why* the contract
    was surfaced, so a re-scored parent produces a distinct notification.
    """
    modes = option.get("strategy_modes") or []
    if isinstance(modes, str):
        modes = [modes]
    normalized_modes = sorted({_norm_upper(mode) for mode in modes if _norm(mode)})
    return json.dumps(
        {
            "modes": normalized_modes,
            "parent_score": _norm_number(option.get("parent_score")),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def stock_delivery_key(recipient: Any, stock: dict[str, Any]) -> str:
    """Stable per-recipient identity for one WIN fire or scanner daily signal."""
    if _norm(stock.get("source")).lower() == "scanner":
        return scanner_delivery_key(recipient, stock)
    pro_symbol, fire_time, resolution = stock_fire_key(stock)
    return _digest({
        "v": SCHEMA_VERSION,
        "kind": KIND_STOCK,
        "recipient": _norm(recipient),
        "pro_symbol": pro_symbol,
        "fire_time": fire_time,
        "resolution": resolution,
    })


def scanner_delivery_key(recipient: Any, stock: dict[str, Any]) -> str:
    """One scanner notice per recipient/symbol/direction/market-local date."""
    return _digest({
        "v": SCHEMA_VERSION,
        "kind": KIND_STOCK,
        "source": "scanner",
        "recipient": _norm(recipient),
        "market": _norm_upper(stock.get("market")),
        "symbol": _norm_upper(stock.get("pro_symbol") or stock.get("symbol")),
        "direction": _norm_upper(stock.get("direction") or stock.get("signal")),
        "session_date": _norm(stock.get("session_date")),
    })


def option_delivery_key(recipient: Any, option: dict[str, Any]) -> str:
    """Preserve legacy WIN identity; scanner US contracts use immutable terms."""
    if _norm(option.get("source")).lower() == "scanner":
        from tradingview.scheduler.us_options_policy import scanner_option_identity
        return _digest({"v": SCHEMA_VERSION,
                        "scanner_option": scanner_option_identity(recipient, option)})
    return _digest({
        "v": SCHEMA_VERSION,
        "kind": KIND_OPTION,
        "recipient": _norm(recipient),
        "contract": list(option_contract_key(option)),
        "policy": option_policy_key(option),
    })


def summary_delivery_key(recipient: Any, run_id: str, part: int = 0) -> str:
    """Stable per-recipient identity for one cycle summary part.

    The run ID *is* part of this identity: a no-new cycle summary is a
    per-cycle statement, not a per-fire alert, so each cycle may send one.
    """
    return _digest({
        "v": SCHEMA_VERSION,
        "kind": KIND_SUMMARY,
        "recipient": _norm(recipient),
        "run_id": _norm(run_id),
        "part": int(part),
    })


def delivery_key_for(recipient: Any, kind: str, payload: dict[str, Any],
                     run_id: str = "", part: int = 0) -> str:
    """Dispatch to the identity function for *kind*."""
    if kind == KIND_STOCK:
        return stock_delivery_key(recipient, payload)
    if kind == KIND_OPTION:
        return option_delivery_key(recipient, payload)
    if kind == KIND_SUMMARY:
        return summary_delivery_key(recipient, run_id, part)
    raise ValueError(f"unknown notification kind: {kind!r}")


def normalize_recipients(raw: Iterable[Any]) -> list[int]:
    """Return sorted, de-duplicated integer recipient IDs.

    Duplicate IDs must not produce duplicate messages, and non-numeric entries
    are dropped rather than silently coerced.
    """
    recipients: set[int] = set()
    for value in raw:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            recipients.add(value)
            continue
        text = _norm(value)
        if text.lstrip("-").isdigit():
            recipients.add(int(text))
    return sorted(recipients)
