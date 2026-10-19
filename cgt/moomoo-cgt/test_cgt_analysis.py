"""
Unit tests for cgt_analysis.py — Australian CGT FIFO engine.

All tests are network-free. The moomoo module is mocked at the module level
so tests run even when OpenD is not available.
Only pure-logic functions are tested (FifoLedger, helpers, HTML generators).
fetch_deals() and fetch_positions() require OpenD and are not tested here.
"""

import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

# ─── Mock moomoo if not installed ────────────────────────────────────────────
if "moomoo" not in sys.modules:
    _mock = MagicMock()
    _mock.RET_OK = 0
    sys.modules["moomoo"] = _mock

# ─── Import module under test ─────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from cgt_analysis import (
    au_fy,
    fy_range,
    infer_currency,
    FifoLedger,
    fy_summary,
    _fmt,
    df_to_html,
    build_fy_sections,
    generate_html,
)


# ─── au_fy ───────────────────────────────────────────────────────────────────

class TestAuFy(unittest.TestCase):

    def test_july_starts_new_fy(self):
        """1 July 2023 falls in FY2024."""
        self.assertEqual(au_fy(date(2023, 7, 1)), "FY2024")

    def test_june_ends_current_fy(self):
        """30 June 2024 still falls in FY2024 (FY ends June 30)."""
        self.assertEqual(au_fy(date(2024, 6, 30)), "FY2024")

    def test_mid_year_in_same_fy(self):
        """December 2023 falls in FY2024."""
        self.assertEqual(au_fy(date(2023, 12, 31)), "FY2024")

    def test_first_july_2022(self):
        """1 July 2022 is the start of FY2023."""
        self.assertEqual(au_fy(date(2022, 7, 1)), "FY2023")

    def test_june_30_2022(self):
        """30 June 2022 is still in FY2022."""
        self.assertEqual(au_fy(date(2022, 6, 30)), "FY2022")

    def test_january_is_mid_fy(self):
        """January is always mid FY — Jan 2025 is in FY2025."""
        self.assertEqual(au_fy(date(2025, 1, 15)), "FY2025")


# ─── fy_range ────────────────────────────────────────────────────────────────

class TestFyRange(unittest.TestCase):

    def test_fy2024_start(self):
        start, _ = fy_range("FY2024")
        self.assertEqual(start, date(2023, 7, 1))

    def test_fy2024_end(self):
        _, end = fy_range("FY2024")
        self.assertEqual(end, date(2024, 6, 30))

    def test_fy2020_range(self):
        start, end = fy_range("FY2020")
        self.assertEqual(start, date(2019, 7, 1))
        self.assertEqual(end, date(2020, 6, 30))

    def test_roundtrip(self):
        """au_fy of the start date of a range should return the same label."""
        label = "FY2024"
        start, _ = fy_range(label)
        self.assertEqual(au_fy(start), label)


# ─── infer_currency ──────────────────────────────────────────────────────────

class TestInferCurrency(unittest.TestCase):

    def test_au_prefix_gives_aud(self):
        self.assertEqual(infer_currency("AU.CBA"), "AUD")

    def test_us_prefix_gives_usd(self):
        self.assertEqual(infer_currency("US.AAPL"), "USD")

    def test_hk_prefix_gives_hkd(self):
        self.assertEqual(infer_currency("HK.700"), "HKD")

    def test_sg_prefix_gives_sgd(self):
        self.assertEqual(infer_currency("SG.DBS"), "SGD")

    def test_jp_prefix_gives_jpy(self):
        self.assertEqual(infer_currency("JP.1579"), "JPY")

    def test_kr_prefix_gives_krw(self):
        self.assertEqual(infer_currency("KR.005930"), "KRW")

    def test_unknown_prefix_gives_question_marks(self):
        self.assertEqual(infer_currency("FR.AIR"), "???")

    def test_no_prefix_gives_question_marks(self):
        self.assertEqual(infer_currency("AAPL"), "???")

    def test_case_insensitive_prefix(self):
        """Prefix lookup should be case-insensitive due to .upper()."""
        self.assertEqual(infer_currency("au.cba"), "AUD")


# ─── FifoLedger ──────────────────────────────────────────────────────────────

class TestFifoLedgerBuy(unittest.TestCase):

    def test_single_buy_creates_lot(self):
        ledger = FifoLedger()
        ledger.buy("AU.CBA", date(2024, 1, 1), 100.0, 50, "AUD")
        self.assertEqual(len(ledger._lots["AU.CBA"]), 1)

    def test_multiple_buys_accumulate_lots(self):
        ledger = FifoLedger()
        ledger.buy("AU.CBA", date(2024, 1, 1), 100.0, 50, "AUD")
        ledger.buy("AU.CBA", date(2024, 2, 1), 110.0, 20, "AUD")
        self.assertEqual(len(ledger._lots["AU.CBA"]), 2)

    def test_different_tickers_separate_lots(self):
        ledger = FifoLedger()
        ledger.buy("AU.CBA", date(2024, 1, 1), 100.0, 50, "AUD")
        ledger.buy("US.AAPL", date(2024, 1, 1), 150.0, 10, "USD")
        self.assertEqual(len(ledger._lots["AU.CBA"]), 1)
        self.assertEqual(len(ledger._lots["US.AAPL"]), 1)


class TestFifoLedgerShortTermSell(unittest.TestCase):
    """Sell positions held < 365 days → no CGT discount."""

    def setUp(self):
        self.ledger = FifoLedger()
        # Buy 100 shares @ $10.00 on 1 Jan 2024
        self.buy_dt = date(2024, 1, 1)
        self.ledger.buy("AU.CBA", self.buy_dt, 10.0, 100, "AUD")

    def test_exact_sell_matches_lot(self):
        """Selling exact quantity clears the lot."""
        sell_dt = date(2024, 6, 1)   # 152 days < 365
        self.ledger.sell("AU.CBA", sell_dt, 15.0, 100, "AUD")
        self.assertEqual(len(self.ledger.matched), 1)
        self.assertEqual(len(self.ledger._lots["AU.CBA"]), 0)

    def test_sell_records_correct_term(self):
        sell_dt = date(2024, 6, 1)
        self.ledger.sell("AU.CBA", sell_dt, 15.0, 100, "AUD")
        trade = self.ledger.matched[0]
        self.assertEqual(trade["term"], "Short")

    def test_short_term_no_cgt_discount(self):
        sell_dt = date(2024, 6, 1)
        self.ledger.sell("AU.CBA", sell_dt, 15.0, 100, "AUD")
        trade = self.ledger.matched[0]
        self.assertEqual(trade["cgt_discount"], 0.0)
        self.assertAlmostEqual(trade["taxable_gain"], trade["gross_gain"], places=4)

    def test_gross_gain_correct(self):
        sell_dt = date(2024, 6, 1)
        self.ledger.sell("AU.CBA", sell_dt, 15.0, 100, "AUD")
        trade = self.ledger.matched[0]
        # gross_gain = (15 - 10) * 100 = 500
        self.assertAlmostEqual(trade["gross_gain"], 500.0, places=4)

    def test_loss_has_no_discount(self):
        """A loss never gets CGT discount."""
        sell_dt = date(2024, 6, 1)
        self.ledger.sell("AU.CBA", sell_dt, 8.0, 100, "AUD")   # selling at a loss
        trade = self.ledger.matched[0]
        self.assertLess(trade["gross_gain"], 0)
        self.assertEqual(trade["cgt_discount"], 0.0)
        self.assertAlmostEqual(trade["taxable_gain"], trade["gross_gain"], places=4)


class TestFifoLedgerLongTermSell(unittest.TestCase):
    """Sell positions held ≥ 365 days → 50% CGT discount on gains."""

    def setUp(self):
        self.ledger = FifoLedger()
        self.buy_dt = date(2022, 1, 1)
        self.ledger.buy("AU.CBA", self.buy_dt, 10.0, 100, "AUD")

    def _sell_long_term(self, sell_price=15.0):
        sell_dt = date(2023, 6, 1)   # 516 days ≥ 365
        self.ledger.sell("AU.CBA", sell_dt, sell_price, 100, "AUD")
        return self.ledger.matched[-1]

    def test_long_term_term_label(self):
        trade = self._sell_long_term()
        self.assertEqual(trade["term"], "Long")

    def test_long_term_gain_gets_50pct_discount(self):
        trade = self._sell_long_term(sell_price=15.0)
        # gross_gain = (15-10)*100 = 500
        # taxable = 250, discount = 250
        self.assertAlmostEqual(trade["gross_gain"], 500.0, places=4)
        self.assertAlmostEqual(trade["cgt_discount"], 250.0, places=4)
        self.assertAlmostEqual(trade["taxable_gain"], 250.0, places=4)

    def test_long_term_loss_no_discount(self):
        """Long-term losses don't get discount (discount only on gains)."""
        trade = self._sell_long_term(sell_price=8.0)
        self.assertEqual(trade["cgt_discount"], 0.0)
        self.assertAlmostEqual(trade["taxable_gain"], trade["gross_gain"], places=4)


class TestFifoLedgerPartialSell(unittest.TestCase):
    """Partial sells consume oldest lots first."""

    def setUp(self):
        self.ledger = FifoLedger()
        # Two lots: 50 @ $10 (older), 50 @ $12 (newer)
        self.ledger.buy("AU.CBA", date(2022, 1, 1), 10.0, 50, "AUD")
        self.ledger.buy("AU.CBA", date(2022, 6, 1), 12.0, 50, "AUD")

    def test_fifo_order_consumes_oldest_first(self):
        """Selling 60 should consume all 50 of lot 1 + 10 of lot 2."""
        self.ledger.sell("AU.CBA", date(2023, 6, 1), 15.0, 60, "AUD")
        self.assertEqual(len(self.ledger.matched), 2)
        # First match: lot 1 (buy_price=10, qty=50)
        self.assertEqual(self.ledger.matched[0]["buy_price"], 10.0)
        self.assertEqual(self.ledger.matched[0]["qty"], 50)
        # Second match: lot 2 (buy_price=12, qty=10)
        self.assertEqual(self.ledger.matched[1]["buy_price"], 12.0)
        self.assertEqual(self.ledger.matched[1]["qty"], 10)

    def test_remaining_lot_has_correct_qty(self):
        """After partial sell, remaining lot should have correct qty."""
        self.ledger.sell("AU.CBA", date(2023, 6, 1), 15.0, 60, "AUD")
        remaining_lots = self.ledger._lots["AU.CBA"]
        self.assertEqual(len(remaining_lots), 1)
        self.assertAlmostEqual(remaining_lots[0][2], 40.0, places=4)  # 50-10 = 40 remaining

    def test_open_lots_returns_remainder(self):
        self.ledger.sell("AU.CBA", date(2023, 6, 1), 15.0, 60, "AUD")
        open_lots = self.ledger.open_lots()
        total_open = sum(lot["qty"] for lot in open_lots if lot["code"] == "AU.CBA")
        self.assertAlmostEqual(total_open, 40.0, places=4)


class TestFifoLedgerOversell(unittest.TestCase):
    """Selling more than available lots emits a warning."""

    def test_sell_with_no_lots_emits_warning(self):
        ledger = FifoLedger()
        ledger.sell("AU.CBA", date(2024, 1, 1), 15.0, 50, "AUD")
        self.assertEqual(len(ledger.warnings), 1)
        self.assertIn("AU.CBA", ledger.warnings[0])
        self.assertIn("no BUY lots", ledger.warnings[0])

    def test_sell_excess_emits_warning(self):
        ledger = FifoLedger()
        ledger.buy("AU.CBA", date(2023, 1, 1), 10.0, 30, "AUD")
        ledger.sell("AU.CBA", date(2024, 1, 1), 15.0, 50, "AUD")  # 50 > 30 available
        self.assertEqual(len(ledger.warnings), 1)
        self.assertIn("unmatched", ledger.warnings[0])

    def test_no_warning_for_exact_sell(self):
        ledger = FifoLedger()
        ledger.buy("AU.CBA", date(2023, 1, 1), 10.0, 50, "AUD")
        ledger.sell("AU.CBA", date(2024, 1, 1), 15.0, 50, "AUD")
        self.assertEqual(len(ledger.warnings), 0)


class TestFifoLedgerOpenLots(unittest.TestCase):

    def test_open_lots_empty_with_no_buys(self):
        ledger = FifoLedger()
        self.assertEqual(ledger.open_lots(), [])

    def test_open_lots_contains_unsold(self):
        ledger = FifoLedger()
        ledger.buy("AU.CBA", date(2023, 1, 1), 10.0, 100, "AUD")
        lots = ledger.open_lots()
        self.assertEqual(len(lots), 1)
        self.assertEqual(lots[0]["code"], "AU.CBA")
        self.assertEqual(lots[0]["qty"], 100)

    def test_open_lots_has_correct_keys(self):
        ledger = FifoLedger()
        ledger.buy("AU.CBA", date(2023, 1, 1), 10.0, 100, "AUD")
        lot = ledger.open_lots()[0]
        for key in ("code", "currency", "buy_date", "qty", "buy_price", "hold_days", "term"):
            self.assertIn(key, lot)

    def test_long_term_open_lot_labelled_long(self):
        """Lots held ≥ 365 days (from today) should be labelled Long."""
        old_buy = date(2020, 1, 1)   # definitely > 365 days ago
        ledger = FifoLedger()
        ledger.buy("AU.CBA", old_buy, 10.0, 50, "AUD")
        lot = ledger.open_lots()[0]
        self.assertEqual(lot["term"], "Long")


# ─── fy_summary ──────────────────────────────────────────────────────────────

class TestFySummary(unittest.TestCase):

    def _make_matched(self):
        return [
            {
                "code": "AU.CBA", "currency": "AUD", "fy": "FY2024",
                "term": "Short", "gross_gain": 500.0, "cgt_discount": 0.0, "taxable_gain": 500.0,
                "buy_date": date(2024, 1, 1), "sell_date": date(2024, 5, 1), "hold_days": 120,
                "qty": 100, "buy_price": 10.0, "sell_price": 15.0,
            },
            {
                "code": "AU.CBA", "currency": "AUD", "fy": "FY2024",
                "term": "Long", "gross_gain": 400.0, "cgt_discount": 200.0, "taxable_gain": 200.0,
                "buy_date": date(2022, 1, 1), "sell_date": date(2024, 5, 1), "hold_days": 851,
                "qty": 100, "buy_price": 5.0, "sell_price": 9.0,
            },
            {
                "code": "US.AAPL", "currency": "USD", "fy": "FY2023",
                "term": "Short", "gross_gain": -200.0, "cgt_discount": 0.0, "taxable_gain": -200.0,
                "buy_date": date(2023, 1, 1), "sell_date": date(2023, 5, 1), "hold_days": 120,
                "qty": 10, "buy_price": 150.0, "sell_price": 130.0,
            },
        ]

    def test_returns_dataframe(self):
        df = fy_summary(self._make_matched())
        self.assertIsInstance(df, pd.DataFrame)

    def test_empty_matched_returns_empty_df(self):
        df = fy_summary([])
        self.assertTrue(df.empty)

    def test_two_currencies_two_rows_per_fy(self):
        df = fy_summary(self._make_matched())
        # FY2024 has AUD, FY2023 has USD → 2 rows
        self.assertEqual(len(df), 2)

    def test_st_gains_correct(self):
        df = fy_summary(self._make_matched())
        fy24_aud = df[(df["FY"] == "FY2024") & (df["Currency"] == "AUD")].iloc[0]
        self.assertAlmostEqual(fy24_aud["ST Gains"], 500.0, places=2)

    def test_lt_gains_correct(self):
        df = fy_summary(self._make_matched())
        fy24_aud = df[(df["FY"] == "FY2024") & (df["Currency"] == "AUD")].iloc[0]
        self.assertAlmostEqual(fy24_aud["LT Gross Gains"], 400.0, places=2)

    def test_cgt_discount_correct(self):
        df = fy_summary(self._make_matched())
        fy24_aud = df[(df["FY"] == "FY2024") & (df["Currency"] == "AUD")].iloc[0]
        self.assertAlmostEqual(fy24_aud["CGT Discount"], 200.0, places=2)

    def test_net_taxable_sums_st_and_lt(self):
        """Net Taxable = ST taxable + LT taxable = 500 + 200 = 700."""
        df = fy_summary(self._make_matched())
        fy24_aud = df[(df["FY"] == "FY2024") & (df["Currency"] == "AUD")].iloc[0]
        self.assertAlmostEqual(fy24_aud["Net Taxable"], 700.0, places=2)

    def test_st_loss_captured(self):
        df = fy_summary(self._make_matched())
        fy23_usd = df[(df["FY"] == "FY2023") & (df["Currency"] == "USD")].iloc[0]
        self.assertAlmostEqual(fy23_usd["ST Losses"], -200.0, places=2)

    def test_has_required_columns(self):
        df = fy_summary(self._make_matched())
        for col in ["FY", "Currency", "ST Gains", "LT Gross Gains", "CGT Discount", "Net Taxable"]:
            self.assertIn(col, df.columns)


# ─── _fmt ────────────────────────────────────────────────────────────────────

class TestFmt(unittest.TestCase):

    def test_positive_value_has_pos_class(self):
        html = _fmt(100.0)
        self.assertIn('class="pos"', html)
        self.assertIn("+", html)

    def test_negative_value_has_neg_class(self):
        html = _fmt(-50.0)
        self.assertIn('class="neg"', html)
        self.assertNotIn("+", html)

    def test_zero_has_no_class(self):
        html = _fmt(0.0)
        self.assertIn('class=""', html)

    def test_decimals_default_two(self):
        html = _fmt(1.5)
        self.assertIn("1.50", html)

    def test_custom_decimals(self):
        html = _fmt(1.5, decimals=0)
        self.assertIn("2", html)   # rounded to 0 decimals

    def test_span_tag_present(self):
        html = _fmt(100.0)
        self.assertTrue(html.startswith("<span"))
        self.assertTrue(html.endswith("</span>"))


# ─── df_to_html ──────────────────────────────────────────────────────────────

class TestDfToHtml(unittest.TestCase):

    def _simple_df(self):
        return pd.DataFrame({"Label": ["A", "B"], "Value": [10.0, -5.0]})

    def test_empty_returns_no_data_message(self):
        html = df_to_html(pd.DataFrame())
        self.assertIn("No data", html)

    def test_contains_table_tags(self):
        html = df_to_html(self._simple_df())
        self.assertIn("<table", html)
        self.assertIn("</table>", html)

    def test_column_headers_present(self):
        html = df_to_html(self._simple_df())
        self.assertIn("<th>Label</th>", html)
        self.assertIn("<th>Value</th>", html)

    def test_numeric_col_applies_fmt(self):
        html = df_to_html(self._simple_df(), numeric_cols=["Value"])
        # Positive value should have pos class
        self.assertIn('class="pos"', html)
        # Negative value should have neg class
        self.assertIn('class="neg"', html)

    def test_discount_col_applies_disc_class(self):
        df = pd.DataFrame({"Discount": [100.0]})
        html = df_to_html(df, discount_cols=["Discount"])
        self.assertIn('class="disc"', html)


# ─── build_fy_sections ───────────────────────────────────────────────────────

class TestBuildFySections(unittest.TestCase):

    def _make_matched(self):
        return [{
            "code": "AU.CBA", "currency": "AUD", "fy": "FY2024",
            "term": "Short", "gross_gain": 500.0, "cgt_discount": 0.0, "taxable_gain": 500.0,
            "buy_date": date(2024, 1, 1), "sell_date": date(2024, 5, 1), "hold_days": 120,
            "qty": 100, "buy_price": 10.0, "sell_price": 15.0,
        }]

    def test_empty_returns_empty_string(self):
        self.assertEqual(build_fy_sections([]), "")

    def test_contains_fy_label(self):
        html = build_fy_sections(self._make_matched())
        self.assertIn("FY2024", html)

    def test_contains_table_html(self):
        html = build_fy_sections(self._make_matched())
        self.assertIn("<table", html)

    def test_contains_fy_section_class(self):
        html = build_fy_sections(self._make_matched())
        self.assertIn('class="fy-section"', html)


# ─── generate_html ───────────────────────────────────────────────────────────

class TestGenerateHtml(unittest.TestCase):

    def _minimal_args(self):
        matched = [{
            "code": "AU.CBA", "currency": "AUD", "fy": "FY2024",
            "term": "Short", "gross_gain": 500.0, "cgt_discount": 0.0, "taxable_gain": 500.0,
            "buy_date": date(2024, 1, 1), "sell_date": date(2024, 5, 1), "hold_days": 120,
            "qty": 100, "buy_price": 10.0, "sell_price": 15.0,
        }]
        fy_sum_df = fy_summary(matched)
        return {
            "fy_sum_df": fy_sum_df,
            "matched": matched,
            "positions_df": pd.DataFrame(),
            "open_lots": [],
            "warnings": [],
            "fetch_start": "2020-01-01",
        }

    def test_returns_string(self):
        html = generate_html(**self._minimal_args())
        self.assertIsInstance(html, str)

    def test_contains_html_boilerplate(self):
        html = generate_html(**self._minimal_args())
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("</html>", html)

    def test_contains_fy2024(self):
        html = generate_html(**self._minimal_args())
        self.assertIn("FY2024", html)

    def test_warnings_appear_in_output(self):
        args = self._minimal_args()
        args["warnings"] = ["Test warning message"]
        html = generate_html(**args)
        self.assertIn("Test warning message", html)

    def test_no_warnings_no_warnings_section(self):
        args = self._minimal_args()
        args["warnings"] = []
        html = generate_html(**args)
        self.assertNotIn("class=\"warn\"", html)


if __name__ == "__main__":
    unittest.main()
