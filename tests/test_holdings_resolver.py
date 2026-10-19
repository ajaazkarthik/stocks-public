"""Unit tests for tradingview/holdings_resolver.py.

Broker symbols are not TradingView symbols. The whole point of this module is
that a holding is either resolved to a real symbol or explicitly excluded --
it must never quietly become a neutral-scored row. These tests pin that down.
"""

import pytest

from path_config import TV, ensure_paths

ensure_paths(TV)

from holdings_resolver import (  # noqa: E402
    HoldingResolution,
    Outcome,
    cache_age_seconds,
    load_positions,
    resolve_holdings,
    resolve_position,
)


def _pos(symbol, exchange="US", broker="Moomoo-AU"):
    return {"symbol": symbol, "exchange": exchange, "broker": broker}


class TestExchangeNormalisation:
    """Symbol search must return scanner prefixes, scoped to the right market."""

    @pytest.mark.parametrize("display,expected", [
        ("NYSE Arca",     "AMEX"),
        ("NYSE ARCA",     "AMEX"),
        ("NYSE American", "AMEX"),
        ("NASDAQ",        "NASDAQ"),
        ("CBOE",          "CBOE"),
    ])
    def test_display_names_map_to_scanner_prefixes(self, display, expected):
        """'NYSE ARCA:XLV' returns no scanner data, which fabricates a score."""
        from input_sources import _normalise_tv_exchange

        assert _normalise_tv_exchange(display) == expected

    def test_us_search_rejects_a_same_named_foreign_listing(self, mocker):
        """US 'SPAL' must not resolve to India's NSE:SPAL."""
        import input_sources

        mocker.patch.object(
            input_sources, "_resolve_exchange_via_fires_cache", return_value="NSE",
        )
        mocker.patch.object(
            input_sources, "_resolve_exchange_via_tv_search", return_value="CBOE",
        )
        result = input_sources.validate_and_normalise(
            "SPAL", allowed_exchanges=input_sources.US_TV_EXCHANGES,
        )
        assert result.pro_symbol == "CBOE:SPAL"

    def test_unconstrained_search_is_unchanged(self, mocker):
        """Existing callers pass no constraint and must keep their behaviour."""
        import input_sources

        mocker.patch.object(
            input_sources, "_resolve_exchange_via_fires_cache", return_value="NSE",
        )
        assert input_sources.validate_and_normalise("SPAL").pro_symbol == "NSE:SPAL"

    def test_us_holdings_constrain_the_lookup(self, mocker):
        """The resolver must pass the US constraint through for US positions."""
        import input_sources

        spy = mocker.patch.object(
            input_sources, "validate_and_normalise",
            return_value=input_sources.ValidationResult("CBOE:SPAL", ""),
        )
        resolve_position(_pos("ZZTOP", exchange="US"))
        assert spy.call_args.kwargs["allowed_exchanges"] == input_sources.US_TV_EXCHANGES


class TestLeveragedWrappers:
    """Single-stock leveraged ETFs are scored through their underlying."""

    @pytest.mark.parametrize("ticker,expected", [
        ("NVDL", "NASDAQ:NVDA"),
        ("NVDU", "NASDAQ:NVDA"),
        ("MSFU", "NASDAQ:MSFT"),
        ("GGLL", "NASDAQ:GOOGL"),
        ("PTIR", "NASDAQ:PLTR"),
        ("OKLL", "NYSE:OKLO"),
    ])
    def test_known_wrappers_resolve_to_underlying(self, ticker, expected):
        res = resolve_position(_pos(ticker))
        assert res.outcome is Outcome.UNDERLYING
        assert res.pro_symbol == expected

    @pytest.mark.parametrize("ticker,expected", [
        ("APPX", "NASDAQ:APP"),     # AppLovin, NOT Apple
        ("AVGX", "NASDAQ:AVGO"),
        ("CWVX", "NASDAQ:CRWV"),
        ("KLAG", "NASDAQ:KLAC"),
        ("LRCU", "NASDAQ:LRCX"),
        ("NOWL", "NYSE:NOW"),
        ("NVOX", "NYSE:NVO"),
    ])
    def test_newly_mapped_wrappers(self, ticker, expected):
        res = resolve_position(_pos(ticker))
        assert res.outcome is Outcome.UNDERLYING
        assert res.pro_symbol == expected

    def test_appx_is_applovin_not_apple(self):
        """Ticker-shape guessing would map APPX to Apple. It is AppLovin."""
        assert resolve_position(_pos("APPX")).pro_symbol != "NASDAQ:AAPL"

    def test_sgx_dlc_resolves_to_its_underlying(self):
        res = resolve_position(_pos("SGX:Z8TW", exchange="SGX", broker="Moomoo-SG"))
        assert res.outcome is Outcome.UNDERLYING
        assert res.pro_symbol == "NASDAQ:AVGO"

    def test_wrapper_detected_before_qualified_symbol_rule(self):
        """An exchange-qualified wrapper must still resolve to the underlying."""
        res = resolve_position(_pos("SGX:USAW", exchange="SGX"))
        assert res.outcome is Outcome.UNDERLYING
        assert res.pro_symbol == "NASDAQ:AMZN"


class TestFunds:
    """Index/sector funds have no equity fundamentals to score."""

    @pytest.mark.parametrize("ticker", ["XLV", "SOXL", "CURE", "SHNY", "SPAL"])
    def test_us_funds_flagged(self, mocker, ticker):
        import input_sources

        mocker.patch.object(
            input_sources, "validate_and_normalise",
            return_value=input_sources.ValidationResult(f"AMEX:{ticker}", ""),
        )
        assert resolve_position(_pos(ticker)).outcome is Outcome.FUND

    def test_us_fund_is_exchange_qualified(self, mocker):
        """An unprefixed ticker returns no TV screener data, which fabricates a
        neutral score. Funds must be exchange-qualified like any other symbol."""
        import input_sources

        mocker.patch.object(
            input_sources, "validate_and_normalise",
            return_value=input_sources.ValidationResult("AMEX:XLV", ""),
        )
        res = resolve_position(_pos("XLV"))
        assert res.pro_symbol == "AMEX:XLV"
        assert ":" in res.pro_symbol

    def test_unresolvable_fund_is_excluded_not_scored(self, mocker):
        import input_sources

        mocker.patch.object(
            input_sources, "validate_and_normalise",
            return_value=input_sources.ValidationResult("", "no match"),
        )
        res = resolve_position(_pos("XLV"))
        assert res.outcome is Outcome.EXCLUDED
        assert not res.scoreable

    def test_index_dlc_without_underlying_is_a_fund(self):
        assert resolve_position(_pos("SGX:ZOGW", exchange="SGX")).outcome is Outcome.FUND

    def test_hk_numeric_fund_is_zero_stripped_and_prefixed(self):
        res = resolve_position(_pos("07200", exchange="HK", broker="Webull AU"))
        assert res.outcome is Outcome.FUND
        assert res.pro_symbol == "HKEX:7200"

    def test_asx_fund_gets_region_prefix(self):
        res = resolve_position(_pos("GEAR", exchange="AU", broker="Webull AU"))
        assert res.outcome is Outcome.FUND
        assert res.pro_symbol == "ASX:GEAR"


class TestPlainSymbols:
    def test_already_qualified_passes_through(self):
        res = resolve_position(_pos("NSE:POLYCAB", exchange="NSE", broker="Zerodha"))
        assert res.outcome is Outcome.EQUITY
        assert res.pro_symbol == "NSE:POLYCAB"

    @pytest.mark.parametrize("exchange,ticker,expected", [
        ("AU", "AUB", "ASX:AUB"),
        ("ASX", "TNE", "ASX:TNE"),
        ("NSE", "TITAN", "NSE:TITAN"),
        ("SGX", "H22", "SGX:H22"),
        ("TYO", "2865", "TYO:2865"),
        ("JP", "7203", "TYO:7203"),
    ])
    def test_single_exchange_regions_need_no_network(self, exchange, ticker, expected):
        res = resolve_position(_pos(ticker, exchange=exchange))
        assert res.outcome is Outcome.EQUITY
        assert res.pro_symbol == expected

    def test_us_bare_ticker_uses_symbol_search(self, mocker):
        """US region cannot disambiguate NASDAQ/NYSE, so it must look the symbol up."""
        import input_sources

        mocker.patch.object(
            input_sources, "validate_and_normalise",
            return_value=input_sources.ValidationResult("NASDAQ:SMCI", ""),
        )
        res = resolve_position(_pos("SMCI", exchange="US"))
        assert res.outcome is Outcome.EQUITY
        assert res.pro_symbol == "NASDAQ:SMCI"


class TestExclusions:
    """Anything unresolvable must be excluded, never silently scored."""

    def test_unresolvable_us_ticker_is_excluded(self, mocker):
        import input_sources

        mocker.patch.object(
            input_sources, "validate_and_normalise",
            return_value=input_sources.ValidationResult("", "could not resolve exchange"),
        )
        res = resolve_position(_pos("ZZZZ", exchange="US"))
        assert res.outcome is Outcome.EXCLUDED
        assert not res.scoreable
        assert res.detail

    def test_empty_symbol_is_excluded(self):
        res = resolve_position(_pos(""))
        assert res.outcome is Outcome.EXCLUDED

    def test_excluded_holdings_never_carry_a_symbol(self, mocker):
        import input_sources

        mocker.patch.object(
            input_sources, "validate_and_normalise",
            return_value=input_sources.ValidationResult("", "nope"),
        )
        assert resolve_position(_pos("ZZZZ", exchange="US")).pro_symbol == ""


class TestDeduplication:
    def test_same_symbol_at_two_brokers_scored_once(self):
        positions = [
            _pos("NSE:TITAN", exchange="NSE", broker="Zerodha"),
            _pos("NSE:TITAN", exchange="NSE", broker="AngelOne"),
        ]
        assert len(resolve_holdings(positions)) == 1

    def test_two_wrappers_on_one_underlying_scored_once(self):
        """NVDL and NVDU both mean NVDA -- scoring it twice would double-count."""
        resolutions = resolve_holdings([_pos("NVDL"), _pos("NVDU")])
        assert len(resolutions) == 1
        assert resolutions[0].pro_symbol == "NASDAQ:NVDA"

    def test_exclusions_are_all_reported(self, mocker):
        """Excluded rows must not be deduped away -- each needs its own reason."""
        import input_sources

        mocker.patch.object(
            input_sources, "validate_and_normalise",
            return_value=input_sources.ValidationResult("", "nope"),
        )
        resolutions = resolve_holdings([
            _pos("ZZZZ", exchange="US"), _pos("YYYY", exchange="US"),
        ])
        assert len(resolutions) == 2
        assert all(r.outcome is Outcome.EXCLUDED for r in resolutions)


class TestCacheAccess:
    def test_missing_cache_returns_empty(self, tmp_path):
        assert load_positions(tmp_path / "nope.json") == []

    def test_malformed_cache_returns_empty(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert load_positions(bad) == []

    def test_age_is_none_when_cache_missing(self, tmp_path):
        assert cache_age_seconds(tmp_path / "nope.json") is None

    def test_age_reflects_saved_at(self, tmp_path):
        import json
        import time

        cache = tmp_path / "c.json"
        cache.write_text(json.dumps({"saved_at": time.time() - 7200, "positions": []}),
                         encoding="utf-8")
        age = cache_age_seconds(cache)
        assert age is not None and 7100 < age < 7300


class TestResolutionContract:
    def test_scoreable_requires_a_symbol(self):
        assert not HoldingResolution("X", "", "b", Outcome.EQUITY).scoreable
        assert HoldingResolution("X", "NASDAQ:X", "b", Outcome.EQUITY).scoreable

    def test_excluded_is_never_scoreable(self):
        assert not HoldingResolution("X", "NASDAQ:X", "b", Outcome.EXCLUDED).scoreable

    def test_broker_is_preserved(self):
        res = resolve_position(_pos("NSE:TITAN", exchange="NSE", broker="Zerodha"))
        assert res.broker == "Zerodha"

    def test_source_symbol_is_preserved(self):
        assert resolve_position(_pos("NVDL")).source_symbol == "NVDL"
