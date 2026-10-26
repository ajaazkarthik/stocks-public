"""Manual, no-trade, no-persistence research CLI. NEVER imported by scheduler."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from tradingview.options.paper_scanner import POLICY
from tradingview.options.paper_scanner_data import fetch_full_research_observation
from tradingview.options.zerodte.config import DEFAULT_CONFIG
from tradingview.options.zerodte.fetcher import _load_tv_cookies, _make_tv_headers


def main(argv=None):
    ap=argparse.ArgumentParser(description='READ-ONLY US options provider parity and TQS scoring; never submits orders')
    ap.add_argument('--symbol',choices=POLICY['universe'],default='AMEX:SPY')
    ap.add_argument('--max-quotes',type=int,default=4)
    ap.add_argument('--tv-lenses',action='store_true')
    ap.add_argument('--cboe',action='store_true')
    ap.add_argument('--smile',action='store_true')
    ap.add_argument('--moomoo',action='store_true')
    args=ap.parse_args(argv)
    if args.max_quotes not in range(1,25):ap.error('--max-quotes must be 1 to 24')
    headers=None
    if args.tv_lenses:
        headers=_make_tv_headers(_load_tv_cookies(DEFAULT_CONFIG.cookie_path))
        if not _load_tv_cookies(DEFAULT_CONFIG.cookie_path).get('sessionid'):
            ap.error('TV authenticated cookie missing; no anonymous fallback')
    now=datetime.now(timezone.utc)
    # Options discovery is always read-only, but may return no quotes outside
    # a supported underlying session/expiry, fail closed (nonzero exit).
    obs=fetch_full_research_observation(args.symbol,[],now,headers,
            enable_tv_lenses=args.tv_lenses,enable_cboe=args.cboe,
            enable_smile=args.smile,enable_moomoo=args.moomoo,
            max_quotes=args.max_quotes)
    comparisons=obs['source_comparisons']
    output={'symbol':args.symbol,'observed_at':obs['observed_at'],
            'underlying_quote_count':len(obs['quotes']),
            'source_note':obs['source_note'],
            'comparisons':comparisons,
            'lenses':obs.get('research_lenses'),
            'note':'reference observations, NEVER executable fills or profit; no orders, no DB writes'}
    print(json.dumps(output,indent=2,sort_keys=True,allow_nan=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
