"""Independent pre-Astra characterization tests: *known* review failures.

The xfail(strict=True) markers are deliberately temporary: these tests encode
desired safety/correctness properties that original PR #34 does not satisfy.
Once fixed, remove xfail markers and require a passing assertion.
No network, orders, Telegram or source DB writes.
"""
from datetime import datetime, timezone, timedelta

import pytest

from tests.test_option_paper_scanner import NOW, observation, quote
from tradingview.options.paper_scanner import choose_candidate
from tradingview.options.edge_engine import enumerate_frozen_candidates
from tradingview.options.existing_data_shadow import safe_comparison
from tradingview.options.shadow_repricing import one_arm


@pytest.mark.xfail(strict=True, reason="REVIEW-F01: reference reprice allowed beyond 21d and past option expiry")
def test_hypothetical_reference_mark_not_allowed_after_expiry():
    obs=observation()
    candidate,status=choose_candidate(obs,NOW)
    assert status=="CANDIDATE"
    date_after_expiry=datetime(2026,12,7,15,tzinfo=timezone.utc)
    mark_status,mark=one_arm(candidate,observation(at=date_after_expiry),date_after_expiry)
    # A future exact contract quote should never count as a live, unexpired
    # followup under v1's exit-by-horizon / option-expiry policy.
    assert mark is None
    assert mark_status in ("EXPIRED_OR_PAST_HORIZON","EXACT_FUTURE_QUOTE_UNAVAILABLE")


@pytest.mark.xfail(strict=True, reason="REVIEW-F02: 12-candidate research cap silently prevents ranking full frozen universe")
def test_many_eligible_spreads_can_still_receive_complete_shadow_ranking():
    obs=observation()
    for idx in range(1,20):
        if idx==4:
            continue
        obs["quotes"].append(
            quote(100+idx*.5,"CALL",at=NOW,delta=.55,bid=2,ask=2.04))
    baseline,reason=choose_candidate(obs,NOW)
    assert reason=="CANDIDATE"
    candidates,_=enumerate_frozen_candidates(obs,NOW)
    assert len(candidates)>12
    report=safe_comparison(obs,None,NOW)
    assert report["baseline_status"]=="CANDIDATE"
    # This is the intended future behavior: all eligible candidates remain
    # accounted for, even if valuation is chunked due to compute budget.
    assert report["challenger_status"]!="CANDIDATE_SET_EXCEEDS_RESEARCH_LIMIT"
