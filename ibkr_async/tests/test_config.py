"""
Unit tests for ibkr_async.config.IBKRConfig

Tests environment-variable driven construction, factory methods,
and serialisation helpers.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent.parent))

from ibkr_async.config import (
    IBKRConfig,
    PORT_GW_LIVE,
    PORT_GW_PAPER,
    PORT_TWS_LIVE,
    PORT_TWS_PAPER,
)


class TestIBKRConfigDefaults:

    def test_default_host(self):
        c = IBKRConfig()
        assert c.host == "127.0.0.1"

    def test_default_port_is_live_tws(self):
        c = IBKRConfig()
        assert c.port == PORT_TWS_LIVE == 7496

    def test_default_client_id(self):
        assert IBKRConfig().client_id == 10

    def test_default_timeout(self):
        assert IBKRConfig().timeout == 30

    def test_default_not_readonly(self):
        assert IBKRConfig().readonly is False

    def test_default_not_paper(self):
        assert IBKRConfig().paper_trading is False

    def test_default_market_data_type_is_delayed(self):
        # 3 = delayed, avoids subscription errors
        assert IBKRConfig().market_data_type == 3

    def test_port_constants(self):
        assert PORT_TWS_LIVE  == 7496
        assert PORT_TWS_PAPER == 7497
        assert PORT_GW_LIVE   == 4001
        assert PORT_GW_PAPER  == 4002


class TestIBKRConfigFromEnv:

    def test_from_env_defaults(self, monkeypatch):
        # Clear all IBKR_ env vars so defaults apply
        for key in list(os.environ.keys()):
            if key.startswith("IBKR_"):
                monkeypatch.delenv(key, raising=False)

        c = IBKRConfig.from_env()
        assert c.host      == "127.0.0.1"
        assert c.port      == PORT_TWS_LIVE
        assert c.client_id == 10
        assert c.timeout   == 30
        assert c.readonly  is False
        assert c.paper_trading is False

    def test_from_env_custom_host(self, monkeypatch):
        monkeypatch.setenv("IBKR_HOST", "192.168.1.10")
        c = IBKRConfig.from_env()
        assert c.host == "192.168.1.10"

    def test_from_env_custom_port(self, monkeypatch):
        monkeypatch.setenv("IBKR_PORT", "4001")
        c = IBKRConfig.from_env()
        assert c.port == 4001

    def test_from_env_readonly_flag(self, monkeypatch):
        monkeypatch.setenv("IBKR_READONLY", "1")
        assert IBKRConfig.from_env().readonly is True

    def test_from_env_paper_uses_paper_port(self, monkeypatch):
        monkeypatch.delenv("IBKR_PORT", raising=False)
        monkeypatch.setenv("IBKR_PAPER_TRADING", "1")
        c = IBKRConfig.from_env()
        assert c.paper_trading is True
        assert c.port == PORT_TWS_PAPER

    def test_from_env_explicit_port_overrides_paper_default(self, monkeypatch):
        monkeypatch.setenv("IBKR_PAPER_TRADING", "1")
        monkeypatch.setenv("IBKR_PORT", "4002")
        c = IBKRConfig.from_env()
        assert c.port == 4002

    def test_from_env_custom_client_id(self, monkeypatch):
        monkeypatch.setenv("IBKR_CLIENT_ID", "42")
        assert IBKRConfig.from_env().client_id == 42

    def test_from_env_flex_token(self, monkeypatch):
        monkeypatch.setenv("IBKR_FLEX_TOKEN", "mytoken123")
        assert IBKRConfig.from_env().flex_token == "mytoken123"

    def test_from_env_flex_query_id(self, monkeypatch):
        monkeypatch.setenv("IBKR_FLEX_QUERY_ID", "9999999")
        assert IBKRConfig.from_env().flex_query_id == "9999999"

    def test_from_env_market_data_type(self, monkeypatch):
        monkeypatch.setenv("IBKR_MARKET_DATA_TYPE", "1")
        assert IBKRConfig.from_env().market_data_type == 1


class TestIBKRConfigForTesting:

    def test_for_testing_uses_paper_port(self):
        c = IBKRConfig.for_testing()
        assert c.port == PORT_TWS_PAPER

    def test_for_testing_client_id_99(self):
        assert IBKRConfig.for_testing().client_id == 99

    def test_for_testing_readonly(self):
        assert IBKRConfig.for_testing().readonly is True

    def test_for_testing_paper_flag(self):
        assert IBKRConfig.for_testing().paper_trading is True

    def test_for_testing_short_timeout(self):
        assert IBKRConfig.for_testing().timeout == 5

    def test_for_testing_flex_credentials(self):
        c = IBKRConfig.for_testing()
        assert c.flex_token    == "test_flex_token"
        assert c.flex_query_id == "test_query_id"


class TestIBKRConfigForPaper:

    def test_for_paper_port(self):
        c = IBKRConfig.for_paper()
        assert c.port == PORT_TWS_PAPER

    def test_for_paper_flag(self):
        assert IBKRConfig.for_paper().paper_trading is True


class TestIBKRConfigHelpers:

    def test_connection_string_live(self):
        c = IBKRConfig(host="10.0.0.1", port=7496, client_id=5, paper_trading=False)
        cs = c.connection_string
        assert "10.0.0.1:7496" in cs
        assert "live" in cs

    def test_connection_string_paper(self):
        c = IBKRConfig.for_paper()
        cs = c.connection_string
        assert "paper" in cs

    def test_to_dict_masks_flex_token(self):
        c = IBKRConfig(flex_token="581369287293302825272191")
        d = c.to_dict()
        assert "581369287293302825272191" not in d["flex_token"]
        assert "..." in d["flex_token"]  # Masked

    def test_to_dict_shows_query_id(self):
        c = IBKRConfig(flex_query_id="1462304")
        d = c.to_dict()
        assert d["flex_query_id"] == "1462304"

    def test_to_dict_short_token_masked(self):
        c = IBKRConfig(flex_token="abc")
        d = c.to_dict()
        assert d["flex_token"] == "***"
