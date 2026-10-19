"""Candidate exclusion must not masquerade as a provider outage."""
from datetime import datetime, timezone
from unittest.mock import Mock
import pandas as pd
import pytest
from tradingview.scheduler import freshness

NOW = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)

def bars(stamp):
    return pd.DataFrame({'Close': [10.]}, index=pd.DatetimeIndex([stamp]))

def test_missing_today_uses_old_evidence_but_never_accepts_it(monkeypatch):
    ticker = Mock()
    ticker.history.side_effect = [freshness.yf.exceptions.YFPricesMissingError('NOL.AX', 'no prices'),
                                 bars('2026-10-16T05:00:00Z')]
    monkeypatch.setattr(freshness.yf, 'Ticker', lambda symbol: ticker)
    rows, rejected = freshness.validate_scanner_fires(
        [{'tv_symbol': 'ASX:NOL', 'close': 10}], market='AU',
        session_date='2026-10-19', observed_at=NOW)
    assert not rows
    assert 'session date' in rejected['NOL']
    assert not rejected.provider_errors
    assert [c.kwargs['period'] for c in ticker.history.call_args_list] == ['1d', '5d']

def test_valid_candidate_survives_other_candidate_exclusion():
    rows, rejected = freshness.validate_scanner_fires(
        [{'tv_symbol': 'ASX:NOL', 'close': 10}, {'tv_symbol': 'ASX:CSL', 'close': 10}],
        market='AU', session_date='2026-10-19', observed_at=NOW,
        provider_fetcher=lambda symbol: bars('2026-10-16T05:00:00Z' if symbol=='NOL.AX' else '2026-10-18T23:45:00Z'))
    assert [r['tv_symbol'] for r in rows] == ['ASX:CSL']
    assert list(rejected) == ['NOL']
    assert not rejected.provider_errors

@pytest.mark.parametrize('failure', [TimeoutError('timeout'), RuntimeError('429')])
def test_provider_failure_is_separate_and_not_silenced(failure):
    def fetch(symbol):
        raise failure
    rows, rejected = freshness.validate_scanner_fires(
        [{'tv_symbol': 'ASX:CSL', 'close': 10}], market='AU',
        session_date='2026-10-19', observed_at=NOW, provider_fetcher=fetch)
    assert not rows
    assert 'CSL' in rejected.provider_errors

def test_provider_timeout_does_not_trigger_history_fallback(monkeypatch):
    ticker = Mock()
    ticker.history.side_effect = TimeoutError('timeout')
    monkeypatch.setattr(freshness.yf, 'Ticker', lambda symbol: ticker)
    with pytest.raises(TimeoutError):
        freshness._fetch_yahoo_history('CSL.AX')
    assert ticker.history.call_count == 1
