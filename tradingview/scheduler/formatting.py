"""Safe Telegram message rendering and delivery classification.

The scheduler previously interpolated raw error text into legacy Markdown
delimiters. A routine message such as ``_snapshot run_id does not match current
run_`` contains an unmatched ``_``, and Telegram rejected the whole message with
``Can't parse entities``. The send exception was logged and swallowed, so the
summary silently disappeared.

Rules enforced here:

* Dynamic text is HTML-escaped and sent with ``parse_mode=HTML``.
* Every message part is independently valid — tags are balanced per part.
* Long content is split at line boundaries with room reserved for the wrapper
  and part label, measured in UTF-16 code units (Telegram's own unit).
* A confirmed *formatting* rejection retries the same content as plain text.
  Permission and token failures are never retried as formatting failures.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Iterable, Sequence

from tradingview.scheduler.config import (
    TELEGRAM_MESSAGE_LIMIT,
    TELEGRAM_SAFE_LIMIT,
    redact,
)

PARSE_MODE_HTML = "HTML"
PARSE_MODE_PLAIN = ""

#: Error classes used to decide whether a retry is appropriate.
ERROR_FORMATTING = "formatting"
ERROR_PERMISSION = "permission"
ERROR_TRANSIENT = "transient"
ERROR_UNKNOWN = "unknown"

_FORMATTING_MARKERS = (
    "can't parse entities",
    "cant parse entities",
    "can't find end of the entity",
    "unsupported start tag",
    "unclosed",
    "entity",
    "parse_mode",
    "can't parse",
)
_PERMISSION_MARKERS = (
    "forbidden",
    "not enough rights",
    "bot was blocked",
    "bot can't initiate",
    "chat not found",
    "user is deactivated",
    "unauthorized",
    "invalid token",
)
_TRANSIENT_MARKERS = (
    "timedout",
    "timeout",
    "networkerror",
    "retryafter",
    "retry after",
    "too many requests",
    "flood",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "connection reset",
    "temporarily unavailable",
)

_PART_LABEL = "Part {index}/{total}"


def telegram_length(text: str) -> int:
    """Return Telegram's own length unit (UTF-16 code units).

    Emoji and other astral-plane characters count as two, so measuring with
    ``len()`` would under-count and produce messages Telegram rejects.
    """
    return len(str(text).encode("utf-16-le")) // 2


def escape_html(value: Any) -> str:
    """Escape dynamic text for ``parse_mode=HTML``."""
    return html.escape(str(value if value is not None else ""), quote=False)


def classify_send_error(exc: BaseException) -> str:
    """Classify a send failure so the caller can choose the right recovery."""
    name = type(exc).__name__.lower()
    text = f"{name} {exc}".lower()
    if any(marker in text for marker in _PERMISSION_MARKERS):
        return ERROR_PERMISSION
    if any(marker in text for marker in _FORMATTING_MARKERS):
        return ERROR_FORMATTING
    if any(marker in text for marker in _TRANSIENT_MARKERS):
        return ERROR_TRANSIENT
    return ERROR_UNKNOWN


def retry_after_seconds(exc: BaseException) -> float | None:
    """Extract Telegram's ``retry_after`` hint when present.

    Handles the installed ``telegram.error.RetryAfter`` (which exposes
    ``retry_after`` as a ``timedelta``) as well as plain numeric attributes and
    a textual fallback.
    """
    for attribute in ("retry_after", "retry_after_seconds"):
        value = getattr(exc, attribute, None)
        if value is None:
            continue
        if isinstance(value, timedelta):
            return value.total_seconds()
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    match = re.search(r"retry[_ ]after[^0-9]{0,10}(\d+(?:\.\d+)?)", str(exc), re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


@dataclass
class MessagePart:
    """One independently valid Telegram message."""

    html_text: str
    plain_text: str
    label: str = ""

    def text_for(self, parse_mode: str) -> str:
        return self.html_text if parse_mode == PARSE_MODE_HTML else self.plain_text

    def length(self, parse_mode: str = PARSE_MODE_HTML) -> int:
        return telegram_length(self.text_for(parse_mode))


@dataclass
class DeliveryReceipt:
    """Per-part delivery outcome."""

    label: str
    status: str
    message_id: int | None = None
    error: str = ""
    parse_mode: str = PARSE_MODE_HTML
    attempts: int = 0
    error_class: str = ERROR_UNKNOWN
    retry_after: float | None = None

    @property
    def delivered(self) -> bool:
        return self.status == "sent"

    @property
    def ambiguous(self) -> bool:
        return self.status == "ambiguous"


@dataclass
class Block:
    """A logical section of a summary message."""

    lines: list[str] = field(default_factory=list)
    preformatted: bool = False
    heading: str = ""


def _wrap_pre(lines: Sequence[str]) -> str:
    return "<pre>" + "\n".join(escape_html(line) for line in lines) + "</pre>"


def _wrap_text(lines: Sequence[str]) -> str:
    return "\n".join(escape_html(line) for line in lines)


def _plain_pre(lines: Sequence[str]) -> str:
    return "\n".join(str(line) for line in lines)


def build_parts(
    blocks: Iterable[Block],
    *,
    limit: int = TELEGRAM_SAFE_LIMIT,
    label_parts: bool = True,
) -> list[MessagePart]:
    """Render *blocks* into independently valid, size-bounded message parts.

    Splitting happens on line boundaries. A single line longer than the budget
    is hard-split so no part can exceed the limit. ``limit`` is measured in
    UTF-16 code units and already excludes the reserved part-label space.
    """
    budget = max(64, int(limit))
    # Reserve room for a part label such as "Part 12/12\n\n" plus the wrapper
    # tags that may be added around a part.
    reserve = telegram_length(_PART_LABEL.format(index=99, total=99)) + 2 if label_parts else 0
    reserve += 16  # <b></b> label wrapper + <pre></pre> block wrapper
    body_budget = max(32, budget - reserve)

    rendered: list[tuple[str, str]] = []  # (html, plain) per logical line group
    for block in blocks:
        if not block.lines:
            continue
        if block.preformatted:
            rendered.append((_wrap_pre(block.lines), _plain_pre(block.lines)))
        else:
            rendered.append((_wrap_text(block.lines), _plain_pre(block.lines)))

    parts: list[MessagePart] = []
    current_html: list[str] = []
    current_plain: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current_html, current_plain, current_len
        if not current_html:
            return
        parts.append(MessagePart(
            html_text="\n".join(current_html),
            plain_text="\n".join(current_plain),
        ))
        current_html, current_plain, current_len = [], [], 0

    for html_chunk, plain_chunk in rendered:
        chunk_len = telegram_length(html_chunk)
        if chunk_len > body_budget:
            flush()
            for piece_html, piece_plain in _split_oversized(html_chunk, plain_chunk, body_budget):
                parts.append(MessagePart(html_text=piece_html, plain_text=piece_plain))
            continue
        separator = 1 if current_html else 0
        if current_len + separator + chunk_len > body_budget:
            flush()
            separator = 0
        current_html.append(html_chunk)
        current_plain.append(plain_chunk)
        current_len += separator + chunk_len

    flush()

    if not parts:
        parts = [MessagePart(html_text="", plain_text="")]

    if label_parts and len(parts) > 1:
        total = len(parts)
        for index, part in enumerate(parts, start=1):
            label = _PART_LABEL.format(index=index, total=total)
            part.label = label
            part.html_text = f"<b>{escape_html(label)}</b>\n\n{part.html_text}"
            part.plain_text = f"{label}\n\n{part.plain_text}"

    # Final safety pass: no part may exceed the configured budget once labels
    # and wrappers are applied. This is a hard guarantee, not an estimate.
    parts = _enforce_limit(parts, budget)
    return parts


def _enforce_limit(parts: list[MessagePart], budget: int) -> list[MessagePart]:
    """Re-split any part whose final measured length exceeds *budget*."""
    result: list[MessagePart] = []
    for part in parts:
        if part.length(PARSE_MODE_HTML) <= budget and part.length(PARSE_MODE_PLAIN) <= budget:
            result.append(part)
            continue
        for piece_html, piece_plain in _split_oversized(
            part.html_text, part.plain_text, budget
        ):
            result.append(MessagePart(html_text=piece_html, plain_text=piece_plain))
    return result


def _split_oversized(html_chunk: str, plain_chunk: str, budget: int) -> list[tuple[str, str]]:
    """Hard-split one over-long line into budget-sized pieces.

    Slicing is driven by the **actual measured length** of each candidate piece
    (UTF-16 units, after escaping and wrapper tags), not by a character count.
    Astral characters such as emoji count as two units, and HTML escaping can
    grow a slice, so a character-count step would overshoot the budget.
    """
    pieces: list[tuple[str, str]] = []
    plain = plain_chunk
    is_pre = html_chunk.startswith("<pre>") and html_chunk.endswith("</pre>")
    wrapper = telegram_length("<pre></pre>") if is_pre else 0
    inner_budget = max(1, budget - wrapper)

    index = 0
    length = len(plain)
    while index < length:
        # Grow the slice while it still fits, then back off one character.
        low, high = index + 1, length
        best = index + 1
        while low <= high:
            mid = (low + high) // 2
            candidate = plain[index:mid]
            candidate_html = escape_html(candidate)
            if is_pre:
                candidate_html = f"<pre>{candidate_html}</pre>"
            if telegram_length(candidate_html) <= inner_budget:
                best = mid
                low = mid + 1
            else:
                high = mid - 1
        slice_plain = plain[index:best]
        slice_html = escape_html(slice_plain)
        if is_pre:
            slice_html = f"<pre>{slice_html}</pre>"
        pieces.append((slice_html, slice_plain))
        index = best
    return pieces


def compact_error_detail(detail: Any) -> str:
    """Summarize candidate-level freshness diagnostics for a phone alert.

    Full provider errors remain in the persisted cycle and scheduler log.
    Normalize both scanner-warning and stock-error wrappers before deduplication.
    """
    text = str(detail)
    match = re.match(
        r"^(?:Scanner )?(US|AU|IN|SG): (?:[^\n]*? — )?"
        r"scanner freshness rejected candidate\(s\): (.*)$", text,
    )
    if not match:
        return text
    market, reasons = match.groups()
    if "Yahoo chart provider unavailable" in reasons:
        explanation = "Yahoo intraday price verification is unavailable for some scan candidates."
    elif "no price data found" in reasons:
        explanation = "Yahoo intraday price data unavailable for some scan candidates."
    else:
        explanation = "Some scan candidates failed price freshness validation."
    return f"{market}: {explanation} Affected candidates were withheld; details in scheduler logs."


def render_error_note(label: str, detail: Any) -> str:
    """Render one failure note as plain text (escaped later by the caller)."""
    return f"⚠️ {label}: {redact(detail)}"


def stock_card(stock: dict) -> str:
    """Compact phone-readable recommendation; one card per delivery event."""
    symbol = stock.get("pro_symbol") or f"{stock.get('exchange', '')}:{stock.get('symbol', '')}"
    direction = stock.get("direction") or stock.get("signal") or "Signal"
    if direction == "SCR":
        direction = "Signal"
    source = str(stock.get("source") or "WIN").upper()
    market = f"/{stock['market']}" if stock.get("market") else ""
    return (f"{symbol} · {direction}\n"
            f"Score {float(stock['score']):.1f}/100 · {source}{market}\n")


def option_card(option: dict) -> str:
    """Keep the contract's distinguishing terms on separate short lines."""
    symbol = f"{option.get('exchange', '')}:{option.get('symbol', '')}"
    kind = option.get("opt_type") or option.get("option_type") or ""
    contract = (option.get("contract_id") or option.get("option_symbol")
                or option.get("occ_symbol") or option.get("contract_symbol"))
    from tradingview.options.options_snapshot import option_quote_fields

    quote = option_quote_fields(option)
    bid, ask = quote.get("bid"), quote.get("ask")
    spread_ok = bid is not None and ask is not None and bid <= ask
    if spread_ok:
        premium, basis = (bid + ask) / 2, "mid"
    elif quote.get("last_price"):
        premium, basis = quote["last_price"], "last trade"
    else:
        premium, basis = quote.get("mid_price"), "reference"
    currency = quote.get("premium_currency", "")
    premium_line = (f"Premium {currency + ' ' if currency else ''}{premium:.2f}/unit · {basis}"
                    if premium is not None else "Premium unavailable")
    if option.get("freshness_status") == "reference_unverified":
        premium_line = (f"Premium {currency + ' ' if currency else ''}{premium:.2f}/unit · reference"
                        if premium is not None else "Premium reference unavailable")
    lines = [f"{symbol} · {kind}",
             f"Strike {option.get('strike', '')} · Expiry {option.get('expiry', '')}",
             premium_line]
    if option.get("freshness_status") == "reference_unverified":
        lines.append("Freshness unverified")
    if spread_ok:
        lines.append(f"Bid {bid:.2f} · Ask {ask:.2f}")
    source_suffix = " · Scanner US" if option.get("source") == "scanner" else ""
    lines.append(f"Score {float(option.get('score', option.get('option_score', 0))):.1f}/100{source_suffix}")
    if option.get("source") == "scanner" and option.get("freshness_status") == "reference_unverified":
        lines.append(f"TradingView update {option.get('tv_update_time')} · not a bid/ask timestamp")
    elif option.get("source") == "scanner" and option.get("bid_quote_at"):
        lines.append(f"Bid quote {option['bid_quote_at']} · {option.get('quote_provider', '')}")
    if contract:
        lines.append(f"Contract {contract}")
    return "\n".join(lines) + "\n"


def render_summary(
    *,
    header: str,
    stock_lines: Sequence[str],
    option_lines: Sequence[str],
    notes: Sequence[str] = (),
    limit: int = TELEGRAM_SAFE_LIMIT,
) -> list[MessagePart]:
    """Render the standard alert summary into safe, bounded parts."""
    if not stock_lines and not option_lines and not notes:
        return []
    blocks: list[Block] = [Block(lines=[header])]
    if stock_lines:
        blocks.extend(Block(lines=[line]) for line in stock_lines)
    if option_lines:
        blocks.extend(Block(lines=[line]) for line in option_lines)
    if notes:
        blocks.append(Block(lines=[render_error_note("note", note) for note in notes]))
    return build_parts(blocks, limit=limit)


async def send_parts(
    bot: Any,
    chat_id: int,
    parts: Sequence[MessagePart],
    *,
    send: Callable[..., Any] | None = None,
    on_receipt: Callable[[MessagePart, DeliveryReceipt], None] | None = None,
    already_delivered: Iterable[str] = (),
) -> list[DeliveryReceipt]:
    """Send each part independently, with a plain-text fallback.

    Parts already acknowledged (by label) are skipped, so a retry never
    re-sends a part the recipient already received.
    """
    delivered_labels = set(already_delivered)
    receipts: list[DeliveryReceipt] = []
    if send is None:
        sender = lambda **kwargs: bot.send_message(**kwargs)  # noqa: E731
    else:
        sender = send

    for part in parts:
        if part.label and part.label in delivered_labels:
            receipts.append(DeliveryReceipt(
                label=part.label, status="sent", parse_mode=PARSE_MODE_HTML,
            ))
            continue
        if not part.html_text.strip():
            continue

        receipt = await _send_one(sender, chat_id, part, PARSE_MODE_HTML)
        if not receipt.delivered and receipt.error_class == ERROR_FORMATTING:
            fallback = await _send_one(sender, chat_id, part, PARSE_MODE_PLAIN)
            fallback.attempts += receipt.attempts
            receipt = fallback
        receipts.append(receipt)
        if on_receipt is not None:
            on_receipt(part, receipt)
    return receipts


async def _send_one(
    sender: Callable[..., Any],
    chat_id: int,
    part: MessagePart,
    parse_mode: str,
) -> DeliveryReceipt:
    text = part.text_for(parse_mode)
    kwargs: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if parse_mode:
        kwargs["parse_mode"] = parse_mode
    try:
        result = sender(**kwargs)
        if hasattr(result, "__await__"):
            result = await result
    except BaseException as exc:  # noqa: BLE001 - classified, never swallowed silently
        return DeliveryReceipt(
            label=part.label,
            status="failed",
            error=redact(f"{type(exc).__name__}: {exc}"),
            parse_mode=parse_mode,
            attempts=1,
            error_class=classify_send_error(exc),
            retry_after=retry_after_seconds(exc),
        )
    message_id = getattr(result, "message_id", None)
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        # No acknowledgement: the remote outcome is unknown, so this must not be
        # recorded as delivered. It stays retryable (at-least-once).
        return DeliveryReceipt(
            label=part.label,
            status="ambiguous",
            error="send returned no message_id acknowledgement",
            parse_mode=parse_mode,
            attempts=1,
            error_class=ERROR_TRANSIENT,
        )
    return DeliveryReceipt(
        label=part.label,
        status="sent",
        message_id=message_id,
        parse_mode=parse_mode,
        attempts=1,
    )


__all__ = [
    "Block",
    "DeliveryReceipt",
    "ERROR_FORMATTING",
    "ERROR_PERMISSION",
    "ERROR_TRANSIENT",
    "ERROR_UNKNOWN",
    "MessagePart",
    "PARSE_MODE_HTML",
    "PARSE_MODE_PLAIN",
    "TELEGRAM_MESSAGE_LIMIT",
    "TELEGRAM_SAFE_LIMIT",
    "build_parts",
    "classify_send_error",
    "escape_html",
    "render_error_note",
    "render_summary",
    "retry_after_seconds",
    "send_parts",
    "telegram_length",
]
