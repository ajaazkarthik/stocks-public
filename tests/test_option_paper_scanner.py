"""Offline lifecycle/economic regressions for the frozen vertical experiment."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from tradingview.options import paper_scanner as ps


NOW = datetime(2026, 10, 26, 15, tzinfo=timezone.utc)
EXPIRY = "2026-12-04"


def features(direction="BULL"):
    return dict(asof="2026-10-23T20:00:00+00:00", close=100.0,
                ema20=99.0 if direction == "BULL" else 101.0,
                ema50=98.0 if direction == "BULL" else 102.0, rv20=.20)


def quote(strike=100.0, option_type="CALL", *, at=NOW, bid=3.0,
          ask=3.04, delta=.55, iv=.20, symbol="AMEX:SPY", **changes):
    q = dict(symbol=symbol, expiry=EXPIRY, strike=strike,
             option_type=option_type, bid=bid, ask=ask, delta=delta, iv=iv,
             spot=100.0, provider="synthetic-fixture", currency="USD",
             multiplier=100, deliverable="REGULAR", contract_spec_source="fixture",
             basis="reference", updated_at=at.isoformat(),
             oi=1000, oi_source="fixture", volume=100,
             volume_session=at.date().isoformat())
    q.update(changes)
    return q


def observation(direction="BULL", kind="DEBIT", *, at=NOW, symbol="AMEX:SPY"):
    opt = ("CALL" if direction == "BULL" else "PUT") if kind == "DEBIT" else ("PUT" if direction == "BULL" else "CALL")
    strike = 100.0 if kind == "DEBIT" else (97.0 if opt == "PUT" else 103.0)
    sign = 1 if opt == "CALL" else -1
    q1 = quote(strike, opt, at=at, delta=sign*(.55 if kind == "DEBIT" else .30),
               iv=.20 if kind == "DEBIT" else .26, symbol=symbol,
               bid=3.0 if kind == "DEBIT" else 1.5,
               ask=3.04 if kind == "DEBIT" else 1.54)
    q2 = quote(strike+sign*2, opt, at=at, delta=sign*.15,
               iv=q1["iv"], bid=2.0 if kind == "DEBIT" else .50,
               ask=2.04 if kind == "DEBIT" else .54, symbol=symbol)
    return dict(symbol=symbol, observed_at=at.isoformat(),
                features=features(direction), quotes=[q1, q2])

