#!/usr/bin/env python3
"""
Webull AU — Gmail Statement Reader + PDF/HTML Parser
=====================================================
Connects to your Gmail inbox via IMAP (App Password — no OAuth required),
finds Webull AU statement emails, downloads PDF attachments, and parses them
into Python dicts (holdings / transactions).

───────────────────────────────────────────────────────────────────────────────
ONE-TIME SETUP
───────────────────────────────────────────────────────────────────────────────
1.  Enable IMAP in Gmail:
        Gmail → Settings → See all settings → Forwarding and POP/IMAP
        → "Enable IMAP" → Save.

2.  Create a Google App Password (required when you have 2-Step Verification):
        https://myaccount.google.com/apppasswords
        Name it "Webull AU Script" → Generate
        Copy the 16-character password (like: abcd efgh ijkl mnop).

3.  Set environment variables:
        $env:GMAIL_ADDRESS      = "you@gmail.com"
        $env:GMAIL_APP_PASSWORD = "abcdefghijklmnop"   # without spaces
        # Optional: IMAP server / port if not Gmail
        # $env:IMAP_HOST = "imap.gmail.com"
        # $env:IMAP_PORT = "993"

4.  Install dependencies:
        pip install pdfplumber

───────────────────────────────────────────────────────────────────────────────
USAGE
───────────────────────────────────────────────────────────────────────────────
    # From Python
    from webull_au_gmail import get_latest_statement, get_all_statements
    stmt = get_latest_statement()
    print(stmt['holdings'])       # list of dicts
    print(stmt['transactions'])   # list of dicts

    # Command line
    python webull_au_gmail.py --list            # list Webull AU emails in inbox
    python webull_au_gmail.py --latest          # parse latest PDF statement
    python webull_au_gmail.py --all             # parse all PDF statements
    python webull_au_gmail.py --save-pdfs ./pdfs # download PDFs to folder, then parse
"""

import email
import email.header
import imaplib
import io
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Optional PDF parser ───────────────────────────────────────────────────────
try:
    import pdfplumber
    _PDF_OK = True
except ImportError:
    _PDF_OK = False

# ── Credentials (env vars with fallback to webull credential file) ────────────
_GMAIL     = os.environ.get("GMAIL_ADDRESS",      "").strip()
_APP_PASS  = os.environ.get("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
_IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com").strip()
_IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))

# Fallback: read from the 'webull' credential file in the same directory
_CRED_FILE = Path(__file__).parent / "webull"
if (_CRED_FILE.exists()) and (not _GMAIL or not _APP_PASS):
    _cred_text = _CRED_FILE.read_text(encoding="utf-8")
    if not _GMAIL:
        _m = re.search(r'GMAIL_ADDRESS\s*=\s*["\']([^"\']+)["\']', _cred_text)
        if _m:
            _GMAIL = _m.group(1).strip()
    if not _APP_PASS:
        _m = re.search(r'GMAIL_APP_PASSWORD\s*=\s*["\']([^"\']+)["\']', _cred_text)
        if _m:
            _APP_PASS = _m.group(1).strip().replace(" ", "")

# ── PDF password (Webull AU PDFs are encrypted with date of birth DDMMYYYY) ──
_PDF_PASSWORD = os.environ.get("WEBULL_AU_PDF_PASSWORD", "").strip()
if not _PDF_PASSWORD and _CRED_FILE.exists():
    _cred_text2 = _CRED_FILE.read_text(encoding="utf-8")
    _m = re.search(r'WEBULL_AU_PDF_PASSWORD\s*=\s*["\']([^"\']+)["\']', _cred_text2)
    if _m:
        _PDF_PASSWORD = _m.group(1).strip()

# ── Search criteria for Webull AU emails ─────────────────────────────────────
# Adjust these if your Webull AU sender address differs.
_WEBULL_SENDERS = [
    "noreply@webull.com.au",
    "service@webull.com.au",
    "support@webull.com.au",
    "statement@webull.com.au",
    "@webull.com.au",          # catches any @webull.com.au address
]
_SUBJECT_KEYWORDS = [
    "statement",
    "holding",
    "portfolio",
    "account summary",
    "activity",
    "trade confirmation",
    "contract note",
]


# ─────────────────────────────────────────────────────────────────────────────
#  IMAP helpers
# ─────────────────────────────────────────────────────────────────────────────

def _connect() -> imaplib.IMAP4_SSL:
    """Open and authenticate an IMAP4_SSL connection to Gmail."""
    if not _GMAIL:
        raise ValueError(
            "GMAIL_ADDRESS not set.\n"
            "Run: $env:GMAIL_ADDRESS = 'you@gmail.com'"
        )
    if not _APP_PASS:
        raise ValueError(
            "GMAIL_APP_PASSWORD not set.\n"
            "Generate one at: https://myaccount.google.com/apppasswords\n"
            "Run: $env:GMAIL_APP_PASSWORD = 'your16charpassword'"
        )
    mail = imaplib.IMAP4_SSL(_IMAP_HOST, _IMAP_PORT)
    mail.login(_GMAIL, _APP_PASS)
    return mail


def _search_webull_emails(mail: imaplib.IMAP4_SSL,
                           sender_filter: str = "",
                           max_results: int = 50) -> list[bytes]:
    """
    Search INBOX for emails from Webull AU.  Returns a list of message IDs.
    Gmail IMAP supports OR searches; we try each sender pattern.
    """
    mail.select("INBOX")
    all_ids: set[bytes] = set()

    senders = [sender_filter] if sender_filter else _WEBULL_SENDERS

    for sender in senders:
        # Build IMAP search criteria — FROM "webull.com.au"
        domain = sender.lstrip("@")
        criteria = f'FROM "{domain}"'
        try:
            status, data = mail.search(None, criteria)
            if status == "OK" and data[0]:
                ids = data[0].split()
                all_ids.update(ids)
        except imaplib.IMAP4.error:
            pass  # Sender pattern produced no results

    if not all_ids:
        # Broader subject-based fallback
        for keyword in _SUBJECT_KEYWORDS:
            criteria = f'SUBJECT "{keyword}"'
            try:
                status, data = mail.search(None, criteria)
                if status == "OK" and data[0]:
                    all_ids.update(data[0].split())
            except imaplib.IMAP4.error:
                pass

    # Sort ascending, return last N
    sorted_ids = sorted(all_ids, key=lambda x: int(x))
    return sorted_ids[-max_results:]


def _fetch_message(mail: imaplib.IMAP4_SSL,
                   msg_id: bytes) -> email.message.Message:
    """Fetch full RFC 2822 message by ID."""
    status, data = mail.fetch(msg_id, "(RFC822)")
    if status != "OK" or not data or not data[0]:
        raise RuntimeError(f"Failed to fetch message {msg_id}")
    raw = data[0][1] if isinstance(data[0], tuple) else data[0]
    return email.message_from_bytes(raw)


def _decode_header_value(value: str) -> str:
    parts = email.header.decode_header(value)
    decoded = []
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _extract_pdfs(msg: email.message.Message) -> list[tuple[str, bytes]]:
    """Return list of (filename, content_bytes) for all PDF attachments."""
    pdfs = []
    for part in msg.walk():
        ct = part.get_content_type()
        disp = part.get("Content-Disposition", "")
        if ct == "application/pdf" or "pdf" in disp.lower():
            filename = part.get_filename() or "statement.pdf"
            filename = _decode_header_value(filename)
            content = part.get_payload(decode=True)
            if content:
                pdfs.append((filename, content))
    return pdfs


def _extract_html_text(msg: email.message.Message) -> str:
    """Return plain-text from HTML or text body parts."""
    body_parts = []
    for part in msg.walk():
        ct = part.get_content_type()
        if ct in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True)
            if payload:
                text = payload.decode(part.get_content_charset() or "utf-8",
                                      errors="replace")
                if ct == "text/html":
                    # Strip tags for naive parsing
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = re.sub(r"&nbsp;", " ", text)
                    text = re.sub(r"&amp;",  "&", text)
                body_parts.append(text)
    return "\n".join(body_parts)


# ─────────────────────────────────────────────────────────────────────────────
#  PDF parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_pdf_bytes(pdf_bytes: bytes,
                    filename: str = "statement.pdf",
                    password: str = "") -> dict:
    """
    Parse a Webull AU PDF statement (bytes) into a dict:
        {
            'filename'     : str,
            'pages'        : int,
            'raw_text'     : str,            # full extracted text
            'holdings'     : list[dict],     # portfolio positions
            'transactions' : list[dict],     # trades / activity
            'account_info' : dict,           # account summary lines
        }

    Webull AU PDFs use a tabular layout; this parser handles common formats:
      • Monthly Account Statement (holdings + cash balances)
      • Trade Confirmation / Contract Note
      • Tax Statement
    """
    if not _PDF_OK:
        raise ImportError(
            "pdfplumber not installed.\n"
            "Run: pip install pdfplumber"
        )

    result: dict = {
        "filename":     filename,
        "pages":        0,
        "raw_text":     "",
        "holdings":     [],
        "transactions": [],
        "account_info": {},
    }

    full_text_parts = []
    all_tables: list[list] = []

    with pdfplumber.open(io.BytesIO(pdf_bytes), password=password or _PDF_PASSWORD or "") as pdf:
        result["pages"] = len(pdf.pages)
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            full_text_parts.append(page_text)
            # Extract tables (list of rows, each row is list of cell strings)
            for table in (page.extract_tables() or []):
                all_tables.append(table)

    result["raw_text"] = "\n".join(full_text_parts)

    # ── Parse holdings from tables ────────────────────────────────────────────
    result["holdings"]     = _parse_holdings_from_tables(all_tables, result["raw_text"])
    result["transactions"] = _parse_transactions_from_tables(all_tables, result["raw_text"])
    result["account_info"] = _parse_account_info(result["raw_text"])

    return result


# -- Holdings table detection --------------------------------------------------
# Webull AU "Portfolio Holdings" section uses columns similar to:
#   Symbol | Security Name | Quantity | Avg Cost | Market Price | Market Value | Unrealized P&L
_HOLDING_COL_PATTERNS = re.compile(
    r"(symbol|ticker|security|instrument|quantity|qty|shares|cost|market\s+value|p&l|gain|loss)",
    re.IGNORECASE,
)

def _parse_holdings_from_tables(tables: list, full_text: str) -> list[dict]:
    holdings = []
    for table in tables:
        if not table or not table[0]:
            continue
        header = [str(c or "").strip().lower() for c in table[0]]
        header_str = " ".join(header)
        if not _HOLDING_COL_PATTERNS.search(header_str):
            continue
        # Map column names → indices
        col = _map_columns(header, {
            "ticker":    ["symbol", "ticker", "code", "instrument", "security name"],
            "name":      ["security name", "name", "description", "instrument name"],
            "qty":       ["quantity", "qty", "shares", "units"],
            "costPrice": ["avg cost", "average cost", "cost price", "cost/share"],
            "mktPrice":  ["market price", "last price", "current price"],
            "mktValue":  ["market value", "value", "current value"],
            "pnl":       ["unrealized p&l", "unrealised p&l", "gain/loss", "p&l",
                          "gain", "profit/loss"],
            "currency":  ["currency", "ccy"],
        })
        for row in table[1:]:
            if not row or all(c is None or str(c).strip() == "" for c in row):
                continue
            h = {}
            for field, idx in col.items():
                if idx is not None and idx < len(row):
                    val = row[idx]
                    h[field] = str(val).strip() if val is not None else ""
            if h.get("ticker") or h.get("name"):
                holdings.append(h)

    # Fallback: regex scan of raw text if no table parsed
    if not holdings:
        holdings = _regex_parse_holdings(full_text)

    return holdings


def _parse_transactions_from_tables(tables: list, full_text: str) -> list[dict]:
    transactions = []
    for table in tables:
        if not table or not table[0]:
            continue
        header = [str(c or "").strip().lower() for c in table[0]]
        header_str = " ".join(header)
        if not re.search(r"(trade date|order|buy|sell|transaction|settlement)", header_str, re.I):
            continue
        col = _map_columns(header, {
            "tradeDate":   ["trade date", "date", "transaction date"],
            "settleDate":  ["settlement date", "settle date"],
            "action":      ["action", "side", "buy/sell", "type"],
            "ticker":      ["symbol", "ticker", "code"],
            "name":        ["security name", "name", "description"],
            "qty":         ["quantity", "qty", "shares", "units"],
            "price":       ["price", "execution price", "trade price"],
            "amount":      ["amount", "net amount", "consideration"],
            "fees":        ["fees", "commission", "brokerage"],
        })
        for row in table[1:]:
            if not row or all(c is None or str(c).strip() == "" for c in row):
                continue
            t = {}
            for field, idx in col.items():
                if idx is not None and idx < len(row):
                    val = row[idx]
                    t[field] = str(val).strip() if val is not None else ""
            if t.get("ticker") or t.get("tradeDate"):
                transactions.append(t)

    return transactions


def _parse_account_info(text: str) -> dict:
    """Extract key account summary lines from raw PDF text."""
    patterns = {
        "accountNumber": r"(?:account\s+(?:number|no\.?|#)\s*[:\-]?\s*)([A-Z0-9\-]+)",
        "accountName":   r"(?:account\s+(?:name|holder)\s*[:\-]?\s*)(.+?)(?:\n|$)",
        "statementDate": r"(?:(?:statement|report|as\s+at|as\s+of)\s+date\s*[:\-]?\s*)(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\d{1,2}\s+\w+\s+\d{4})",
        "totalValue":    r"(?:total\s+(?:portfolio|account)\s+(?:value|assets)\s*[:\-]?\s*\$?\s*)([\d,\.]+)",
        "cashBalance":   r"(?:cash\s+(?:balance|available)\s*[:\-]?\s*(?:AUD)?\s*\$?\s*)([\d,\.]+)",
    }
    info: dict = {}
    for key, pattern in patterns.items():
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            info[key] = m.group(1).strip()
    return info


def _regex_parse_holdings(text: str) -> list[dict]:
    """
    Best-effort regex extraction of holdings when table parsing fails.
    Looks for lines like: CBA  Commonwealth Bank  100  93.50  9,350.00  +250.00
    """
    holdings = []
    # Pattern: ASX-style symbols (2–5 UPPERCASE letters, optionally followed by digits)
    line_pat = re.compile(
        r"^([A-Z]{1,5}\d{0,3})\s+"         # ticker
        r"(.{5,40}?)\s+"                    # name (5–40 chars)
        r"(\d[\d,]*)\s+"                    # qty
        r"([\d,]+\.\d+)\s+"                 # avg cost
        r"([\d,]+\.\d+)\s*"                 # market value / price
        r"([+\-]?\$?\s*[\d,]+\.\d+)?",      # optional P&L
        re.MULTILINE,
    )
    for m in line_pat.finditer(text):
        holdings.append({
            "ticker":    m.group(1),
            "name":      m.group(2).strip(),
            "qty":       m.group(3).replace(",", ""),
            "costPrice": m.group(4).replace(",", ""),
            "mktValue":  m.group(5).replace(",", ""),
            "pnl":       (m.group(6) or "").replace(",", ""),
        })
    return holdings


# ─────────────────────────────────────────────────────────────────────────────
#  Column-map helper
# ─────────────────────────────────────────────────────────────────────────────

def _map_columns(header: list[str], field_synonyms: dict) -> dict[str, Optional[int]]:
    """
    Return {field_name: column_index} by matching field synonyms against header.
    Returns None for any field not found.
    """
    mapping: dict[str, Optional[int]] = {}
    for field, synonyms in field_synonyms.items():
        mapping[field] = None
        for syn in synonyms:
            for i, col in enumerate(header):
                if syn in col:
                    mapping[field] = i
                    break
            if mapping[field] is not None:
                break
    return mapping


# ─────────────────────────────────────────────────────────────────────────────
#  Public interface
# ─────────────────────────────────────────────────────────────────────────────

def list_webull_emails(max_results: int = 20) -> list[dict]:
    """
    Return metadata for recent Webull AU emails:
        [{'id': bytes, 'subject': str, 'from': str, 'date': str, 'has_pdf': bool}, …]
    """
    mail = _connect()
    ids = _search_webull_emails(mail, max_results=max_results)
    results = []
    for mid in ids:
        try:
            msg = _fetch_message(mail, mid)
            has_pdf = any(
                "pdf" in (p.get_content_type() + p.get("Content-Disposition", "")).lower()
                for p in msg.walk()
            )
            results.append({
                "id":      mid,
                "subject": _decode_header_value(msg.get("Subject", "")),
                "from":    msg.get("From", ""),
                "date":    msg.get("Date",  ""),
                "has_pdf": has_pdf,
            })
        except Exception:
            pass
    mail.logout()
    return results


def get_latest_statement(save_pdf_path: Optional[Path] = None) -> dict:
    """
    Fetch and parse the most recent Webull AU PDF statement from Gmail.
    Returns parsed statement dict (see parse_pdf_bytes docstring).
    """
    mail = _connect()
    ids = _search_webull_emails(mail, max_results=50)
    if not ids:
        mail.logout()
        return {"error": "No Webull AU emails found in Gmail INBOX."}

    # Walk from newest to oldest looking for a PDF
    for msg_id in reversed(ids):
        try:
            msg = _fetch_message(mail, msg_id)
            pdfs = _extract_pdfs(msg)
            if not pdfs:
                continue
            filename, pdf_bytes = pdfs[0]
            if save_pdf_path:
                dest = Path(save_pdf_path) / filename
                dest.write_bytes(pdf_bytes)
                print(f"[SAVED] {dest}")
            result = parse_pdf_bytes(pdf_bytes, filename)
            result["email_subject"] = _decode_header_value(msg.get("Subject", ""))
            result["email_date"]    = msg.get("Date", "")
            mail.logout()
            return result
        except Exception as exc:
            print(f"[WARN] Could not parse message {msg_id}: {exc}")

    mail.logout()
    return {"error": "Found Webull AU emails but none contained parseable PDFs."}


def get_all_statements(save_pdf_dir: Optional[Path] = None,
                        max_emails: int = 50) -> list[dict]:
    """
    Fetch and parse ALL Webull AU PDF statements from Gmail.
    Returns list of parsed statement dicts, newest first.
    """
    mail = _connect()
    ids = _search_webull_emails(mail, max_results=max_emails)
    all_results = []

    for msg_id in reversed(ids):
        try:
            msg = _fetch_message(mail, msg_id)
            pdfs = _extract_pdfs(msg)
            if not pdfs:
                # Try parsing HTML body if no PDF
                body_text = _extract_html_text(msg)
                if body_text.strip():
                    # Minimal inline email parse
                    holdings = _regex_parse_holdings(body_text)
                    if holdings:
                        all_results.append({
                            "filename":     "(email body)",
                            "email_subject": _decode_header_value(msg.get("Subject", "")),
                            "email_date":    msg.get("Date", ""),
                            "holdings":      holdings,
                            "transactions":  [],
                            "account_info":  _parse_account_info(body_text),
                        })
                continue
            for filename, pdf_bytes in pdfs:
                if save_pdf_dir:
                    dest = Path(save_pdf_dir) / filename
                    dest.write_bytes(pdf_bytes)
                    print(f"[SAVED] {dest}")
                result = parse_pdf_bytes(pdf_bytes, filename)
                result["email_subject"] = _decode_header_value(msg.get("Subject", ""))
                result["email_date"]    = msg.get("Date", "")
                all_results.append(result)
        except Exception as exc:
            print(f"[WARN] Skipping message {msg_id}: {exc}")

    mail.logout()
    return all_results


# ─────────────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────────────

def _print_holdings_table(holdings: list[dict]) -> None:
    if not holdings:
        print("  (no holdings found in this statement)")
        return
    print(f"\n  {'Ticker':<10} {'Name':<28} {'Qty':>8} {'Cost':>10} {'Mkt Val':>12} {'P&L':>12}")
    print("  " + "-" * 82)
    for h in holdings:
        ticker = h.get("ticker", "?")[:9]
        name   = (h.get("name", "") or "")[:27]
        qty    = h.get("qty", "")
        cost   = h.get("costPrice", "")
        mval   = h.get("mktValue", "")
        pnl    = h.get("pnl", "")
        print(f"  {ticker:<10} {name:<28} {qty:>8} {cost:>10} {mval:>12} {pnl:>12}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Webull AU Gmail statement reader"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list",        action="store_true",
                       help="List recent Webull AU emails")
    group.add_argument("--latest",      action="store_true",
                       help="Fetch and parse the latest PDF statement (default)")
    group.add_argument("--all",         action="store_true",
                       help="Fetch and parse ALL PDF statements")
    parser.add_argument("--save-pdfs",  metavar="DIR",
                        help="Save downloaded PDFs to this directory as well")
    parser.add_argument("--json",       action="store_true",
                        help="Output raw JSON instead of formatted table")
    parser.add_argument("--max",        type=int, default=50,
                        help="Max emails to search (default 50)")
    args = parser.parse_args()

    try:
        save_dir = Path(args.save_pdfs) if args.save_pdfs else None
        if save_dir:
            save_dir.mkdir(parents=True, exist_ok=True)

        if args.list:
            emails = list_webull_emails(max_results=args.max)
            if not emails:
                print("No Webull AU emails found.")
            for e in emails:
                pdf_flag = " [PDF]" if e["has_pdf"] else ""
                print(f"  {e['date'][:20]}  {e['subject'][:50]}{pdf_flag}")
                print(f"    From: {e['from']}")

        elif args.all:
            stmts = get_all_statements(save_pdf_dir=save_dir, max_emails=args.max)
            if args.json:
                print(json.dumps(stmts, indent=2, default=str))
            else:
                for s in stmts:
                    print(f"\n{'='*60}")
                    print(f"  {s.get('email_date', '')[:25]}  {s.get('email_subject', '')}")
                    print(f"  File: {s.get('filename', '')} ({s.get('pages', 0)} pages)")
                    if s.get("account_info"):
                        print(f"  Account: {s['account_info']}")
                    _print_holdings_table(s.get("holdings", []))
                    txns = s.get("transactions", [])
                    if txns:
                        print(f"\n  Transactions ({len(txns)}):")
                        for t in txns[:10]:
                            print(f"    {t}")
        else:
            # Default: --latest
            stmt = get_latest_statement(save_pdf_path=save_dir)
            if "error" in stmt:
                print(f"[ERROR] {stmt['error']}")
                sys.exit(1)
            if args.json:
                print(json.dumps(stmt, indent=2, default=str))
            else:
                print(f"\nStatement: {stmt.get('filename')} ({stmt.get('pages', 0)} pages)")
                print(f"Email:     {stmt.get('email_date', '')[:25]}  {stmt.get('email_subject', '')}")
                if stmt.get("account_info"):
                    print(f"Account:   {stmt['account_info']}")
                _print_holdings_table(stmt.get("holdings", []))
                txns = stmt.get("transactions", [])
                if txns:
                    print(f"\nTransactions ({len(txns)}):")
                    for t in txns[:20]:
                        print(f"  {t}")

    except ValueError as exc:
        print(f"[CONFIG ERROR] {exc}")
        sys.exit(1)
    except imaplib.IMAP4.error as exc:
        print(f"[IMAP ERROR] {exc}")
        print("\nTroubleshooting:")
        print("  1. Check that GMAIL_ADDRESS and GMAIL_APP_PASSWORD are set correctly.")
        print("  2. Enable IMAP in Gmail Settings → Forwarding and POP/IMAP.")
        print("  3. Make sure App Password has no spaces (strip them when setting).")
        sys.exit(1)
