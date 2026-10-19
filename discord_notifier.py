"""
Discord Notifier — Send trading signals and alerts to Discord via webhook.
==========================================================================
Posts signal alerts, portfolio summaries, and system notifications to a Discord
channel using Discord webhook API.

Usage:
    from discord_notifier import DiscordNotifier
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/...")
    notifier.send_signal("AAPL", score=72.5, action="BUY")

    # CLI:
    python discord_notifier.py --test  # sends test message
    python discord_notifier.py --message "Hello from stocks bot"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Constants ──────────────────────────────────────────────────────────────────
WEBHOOK_URL_ENV = "DISCORD_WEBHOOK_URL"
MAX_MESSAGE_LENGTH = 2000
MAX_EMBED_LENGTH = 4096
RATE_LIMIT_DELAY = 1.0  # seconds between messages


@dataclass
class DiscordEmbed:
    """Discord embed object."""
    title: str = ""
    description: str = ""
    color: int = 0x58a6ff  # Blue
    fields: list = field(default_factory=list)
    footer: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        d: dict = {}
        if self.title:
            d["title"] = self.title[:256]
        if self.description:
            d["description"] = self.description[:MAX_EMBED_LENGTH]
        if self.color:
            d["color"] = self.color
        if self.fields:
            d["fields"] = [
                {"name": f["name"][:256], "value": str(f["value"])[:1024], "inline": f.get("inline", True)}
                for f in self.fields[:25]
            ]
        if self.footer:
            d["footer"] = {"text": self.footer[:2048]}
        if self.timestamp:
            d["timestamp"] = self.timestamp
        return d

    def add_field(self, name: str, value: str, inline: bool = True) -> "DiscordEmbed":
        self.fields.append({"name": name, "value": value, "inline": inline})
        return self


class DiscordNotifier:
    """Send notifications to Discord via webhook."""

    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = webhook_url or os.environ.get(WEBHOOK_URL_ENV, "")
        self._last_send_time = 0.0

    def is_configured(self) -> bool:
        """Check if webhook URL is configured."""
        return bool(self.webhook_url and self.webhook_url.startswith("https://discord.com/api/webhooks/"))

    def _rate_limit(self) -> None:
        """Simple rate limiting."""
        elapsed = time.time() - self._last_send_time
        if elapsed < RATE_LIMIT_DELAY:
            time.sleep(RATE_LIMIT_DELAY - elapsed)

    def _send_payload(self, payload: dict) -> tuple[bool, str]:
        """Send a payload to the Discord webhook."""
        if not self.is_configured():
            return False, "Discord webhook URL not configured"

        self._rate_limit()

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "StocksTradingBot/1.0"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                self._last_send_time = time.time()
                status = resp.status
                if status in (200, 204):
                    return True, "OK"
                return False, f"HTTP {status}"
        except urllib.error.HTTPError as e:
            if e.code == 429:  # Rate limited
                retry_after = float(e.headers.get("Retry-After", "5"))
                time.sleep(retry_after)
                # Retry once
                try:
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        self._last_send_time = time.time()
                        return True, "OK (after rate limit)"
                except Exception:
                    pass
            return False, f"HTTP {e.code}: {e.reason}"
        except Exception as e:
            return False, str(e)

    def send_message(self, content: str) -> tuple[bool, str]:
        """Send a simple text message."""
        if len(content) > MAX_MESSAGE_LENGTH:
            content = content[:MAX_MESSAGE_LENGTH - 3] + "..."
        return self._send_payload({"content": content})

    def send_embed(self, embed: DiscordEmbed) -> tuple[bool, str]:
        """Send a rich embed message."""
        return self._send_payload({"embeds": [embed.to_dict()]})

    def send_signal(
        self,
        symbol: str,
        score: float,
        action: str = "BUY",
        l1: float = 0, l2: float = 0, l3: float = 0, l4: float = 0,
        price: float = 0, broker: str = "",
        extra_notes: str = "",
    ) -> tuple[bool, str]:
        """Send a trading signal notification."""
        color = 0x3fb950 if score >= 75 else 0xd29922 if score >= 65 else 0x58a6ff if score >= 55 else 0x8b949e

        emoji = "🟢" if score >= 75 else "🟡" if score >= 65 else "🔵" if score >= 55 else "⚪"

        embed = DiscordEmbed(
            title=f"{emoji} {action} Signal: {symbol}",
            description=f"Composite Score: **{score:.1f}**",
            color=color,
            timestamp=datetime.now(timezone.utc).isoformat() + "Z",
            footer="Stocks Trading System",
        )

        embed.add_field("L1 Technical", f"{l1:.1f}", inline=True)
        embed.add_field("L2 Fundamental", f"{l2:.1f}", inline=True)
        embed.add_field("L3 Sentiment", f"{l3:.1f}", inline=True)
        embed.add_field("L4 Market", f"{l4:.1f}", inline=True)

        if price > 0:
            embed.add_field("Price", f"${price:.2f}", inline=True)
        if broker:
            embed.add_field("Broker", broker, inline=True)
        if extra_notes:
            embed.add_field("Notes", extra_notes, inline=False)

        return self.send_embed(embed)

    def send_portfolio_summary(
        self,
        total_value: float,
        daily_pnl: float,
        broker_summary: dict[str, dict],
    ) -> tuple[bool, str]:
        """Send daily portfolio summary."""
        pnl_emoji = "📈" if daily_pnl >= 0 else "📉"
        color = 0x3fb950 if daily_pnl >= 0 else 0xf85149

        embed = DiscordEmbed(
            title=f"{pnl_emoji} Daily Portfolio Summary",
            description=f"Total: **${total_value:,.0f}** | P&L: **{'+'if daily_pnl>=0 else ''}{daily_pnl:,.0f}**",
            color=color,
            timestamp=datetime.now(timezone.utc).isoformat() + "Z",
            footer="Stocks Trading System",
        )

        for broker, info in broker_summary.items():
            val = info.get("value", 0)
            pnl = info.get("pnl", 0)
            embed.add_field(broker, f"${val:,.0f} ({'+' if pnl>=0 else ''}{pnl:,.0f})", inline=True)

        return self.send_embed(embed)

    def send_alert(self, title: str, message: str, level: str = "info") -> tuple[bool, str]:
        """Send a system alert."""
        colors = {"info": 0x58a6ff, "warn": 0xd29922, "error": 0xf85149, "success": 0x3fb950}
        emojis = {"info": "ℹ️", "warn": "⚠️", "error": "❌", "success": "✅"}

        embed = DiscordEmbed(
            title=f"{emojis.get(level, 'ℹ️')} {title}",
            description=message,
            color=colors.get(level, 0x58a6ff),
            timestamp=datetime.now(timezone.utc).isoformat() + "Z",
        )

        return self.send_embed(embed)


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Discord Notifier for trading signals")
    parser.add_argument("--webhook-url", help="Discord webhook URL (or set DISCORD_WEBHOOK_URL env)")
    parser.add_argument("--test", action="store_true", help="Send a test message")
    parser.add_argument("--message", help="Send a plain text message")
    parser.add_argument("--signal", nargs=2, metavar=("SYMBOL", "SCORE"),
                        help="Send a signal alert (e.g. --signal AAPL 72.5)")
    args = parser.parse_args()

    notifier = DiscordNotifier(webhook_url=args.webhook_url)

    if not notifier.is_configured():
        print(f"❌ Discord webhook URL not configured. Set {WEBHOOK_URL_ENV} env var or use --webhook-url")
        sys.exit(1)

    if args.test:
        ok, msg = notifier.send_alert("Test Message", "Discord integration is working! 🎉", "success")
        print(f"{'✅' if ok else '❌'} Test message: {msg}")

    elif args.message:
        ok, msg = notifier.send_message(args.message)
        print(f"{'✅' if ok else '❌'} Message: {msg}")

    elif args.signal:
        symbol, score = args.signal
        ok, msg = notifier.send_signal(symbol, float(score))
        print(f"{'✅' if ok else '❌'} Signal: {msg}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
