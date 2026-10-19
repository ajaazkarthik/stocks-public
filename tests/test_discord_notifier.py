"""Tests for discord_notifier.py — Discord webhook integration."""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord_notifier as dn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def notifier():
    """Create a DiscordNotifier with a fake webhook URL."""
    return dn.DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123456/fake-token")


@pytest.fixture()
def unconfigured_notifier():
    """Create a DiscordNotifier without a webhook URL."""
    return dn.DiscordNotifier(webhook_url="")


# ---------------------------------------------------------------------------
# Test: DiscordEmbed
# ---------------------------------------------------------------------------

class TestDiscordEmbed:
    def test_to_dict_basic(self):
        embed = dn.DiscordEmbed(title="Test", description="Hello")
        d = embed.to_dict()
        assert d["title"] == "Test"
        assert d["description"] == "Hello"

    def test_add_field(self):
        embed = dn.DiscordEmbed()
        embed.add_field("Name", "Value", inline=True)
        assert len(embed.fields) == 1
        assert embed.fields[0]["name"] == "Name"

    def test_to_dict_with_fields(self):
        embed = dn.DiscordEmbed(title="T")
        embed.add_field("F1", "V1")
        d = embed.to_dict()
        assert len(d["fields"]) == 1

    def test_truncates_long_title(self):
        embed = dn.DiscordEmbed(title="A" * 500)
        d = embed.to_dict()
        assert len(d["title"]) <= 256


# ---------------------------------------------------------------------------
# Test: is_configured
# ---------------------------------------------------------------------------

class TestIsConfigured:
    def test_configured_with_valid_url(self, notifier):
        assert notifier.is_configured() is True

    def test_not_configured_without_url(self, unconfigured_notifier):
        assert unconfigured_notifier.is_configured() is False

    def test_not_configured_with_bad_url(self):
        n = dn.DiscordNotifier(webhook_url="http://not-discord.com")
        assert n.is_configured() is False


# ---------------------------------------------------------------------------
# Test: send_message — returns tuple[bool, str]
# ---------------------------------------------------------------------------

class TestSendMessage:
    @patch("discord_notifier.urllib.request.urlopen")
    def test_sends_text_message(self, mock_urlopen, notifier):
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        ok, msg = notifier.send_message("Hello World")
        assert ok is True
        assert msg == "OK"

    def test_fails_with_no_url(self, unconfigured_notifier):
        ok, msg = unconfigured_notifier.send_message("Hello")
        assert ok is False
        assert "not configured" in msg


# ---------------------------------------------------------------------------
# Test: send_signal — returns tuple[bool, str]
# ---------------------------------------------------------------------------

class TestSendSignal:
    @patch("discord_notifier.urllib.request.urlopen")
    def test_sends_signal_embed(self, mock_urlopen, notifier):
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        ok, msg = notifier.send_signal("AAPL", score=72.5, action="BUY")
        assert ok is True

    @patch("discord_notifier.urllib.request.urlopen")
    def test_signal_with_extra_fields(self, mock_urlopen, notifier):
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        ok, msg = notifier.send_signal(
            "AAPL", score=72.5, action="BUY",
            l1=25.0, l2=22.0, l3=14.0, l4=11.5,
            price=150.0, broker="IBKR",
        )
        assert ok is True


# ---------------------------------------------------------------------------
# Test: send_alert — returns tuple[bool, str]
# ---------------------------------------------------------------------------

class TestSendAlert:
    @patch("discord_notifier.urllib.request.urlopen")
    def test_sends_alert(self, mock_urlopen, notifier):
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        ok, msg = notifier.send_alert("Test Alert", "Something happened", level="warn")
        assert ok is True
        assert msg == "OK"


# ---------------------------------------------------------------------------
# Test: send_portfolio_summary — takes (total_value, daily_pnl, broker_summary)
# ---------------------------------------------------------------------------

class TestSendPortfolioSummary:
    @patch("discord_notifier.urllib.request.urlopen")
    def test_sends_summary(self, mock_urlopen, notifier):
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        ok, msg = notifier.send_portfolio_summary(
            total_value=100000,
            daily_pnl=500,
            broker_summary={"IBKR": {"value": 50000, "pnl": 300}},
        )
        assert ok is True


# ---------------------------------------------------------------------------
# Test: rate limiting
# ---------------------------------------------------------------------------

class TestRateLimiting:
    @patch("discord_notifier.urllib.request.urlopen")
    def test_rate_limit_field_exists(self, mock_urlopen, notifier):
        assert hasattr(notifier, "_last_send_time")
        assert notifier._last_send_time == 0.0


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------

class TestDiscordIntegration:
    @patch("discord_notifier.urllib.request.urlopen")
    def test_full_workflow(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        notifier = dn.DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/123456/fake-token"
        )
        assert notifier.is_configured()

        ok, msg = notifier.send_signal("AAPL", 75.0, "BUY")
        assert ok is True

        ok, msg = notifier.send_message("Test complete")
        assert ok is True
