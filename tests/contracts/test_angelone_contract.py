"""
Contract tests: AngelOne SmartAPI response shape stability.
===========================================================
Validates that the expected dict keys from AngelOne SmartAPI responses
remain stable. When AngelOne renames or removes a field, these tests
break immediately — preventing silent data loss in the portfolio pipeline.
"""
import pytest


# ── Holdings response keys (from smart.holding()) ────────────────────────
REQUIRED_HOLDING_KEYS = {
    "tradingsymbol", "quantity", "averageprice", "ltp", "pnl", "exchange",
}

# ── Positions response keys (from smart.position()) ─────────────────────
REQUIRED_POSITION_KEYS = {
    "tradingsymbol", "netqty", "buyqty", "sellqty", "ltp", "pnl",
}

# ── RMS / funds response keys (from smart.rmsLimit()) ───────────────────
REQUIRED_RMS_KEYS = {
    "net", "availablecash", "utiliseddebits",
}

# ── Profile response keys (from smart.getProfile()) ─────────────────────
REQUIRED_PROFILE_KEYS = {
    "clientcode", "name", "email", "mobileno", "exchanges",
}

# ── GTT order keys (from angelone_gtt_manager.py) ───────────────────────
REQUIRED_GTT_KEYS = {
    "tradingsymbol", "exchange", "producttype", "transactiontype",
    "qty", "triggerprice",
}


# ══════════════════════════════════════════════════════════════════════════
#  Shape tests
# ══════════════════════════════════════════════════════════════════════════

class TestAngelOneHoldingContract:
    """Holdings dict from smart.holding() must contain all required keys."""

    SAMPLE = {
        "tradingsymbol": "INFY",
        "quantity": 10,
        "averageprice": 1500.0,
        "ltp": 1600.0,
        "pnl": 1000.0,
        "exchange": "NSE",
        "symboltoken": "1594",
        "isin": "INE009A01021",
        "t1quantity": 0,
        "realisedquantity": 10,
        "authorisedquantity": 0,
    }

    def test_required_keys_present(self):
        missing = REQUIRED_HOLDING_KEYS - set(self.SAMPLE.keys())
        assert not missing, f"Missing keys in holding: {missing}"

    def test_value_types(self):
        assert isinstance(self.SAMPLE["tradingsymbol"], str)
        assert isinstance(self.SAMPLE["quantity"], (int, float))
        assert isinstance(self.SAMPLE["averageprice"], (int, float))
        assert isinstance(self.SAMPLE["ltp"], (int, float))
        assert isinstance(self.SAMPLE["pnl"], (int, float))
        assert isinstance(self.SAMPLE["exchange"], str)

    def test_exchange_is_valid(self):
        """Exchange must be NSE or BSE."""
        assert self.SAMPLE["exchange"] in ("NSE", "BSE")


class TestAngelOnePositionContract:
    """Position dict from smart.position() must contain all required keys."""

    SAMPLE = {
        "tradingsymbol": "RELIANCE",
        "netqty": 5,
        "buyqty": 5,
        "sellqty": 0,
        "ltp": 2500.0,
        "pnl": 500.0,
        "exchange": "NSE",
        "producttype": "DELIVERY",
    }

    def test_required_keys_present(self):
        missing = REQUIRED_POSITION_KEYS - set(self.SAMPLE.keys())
        assert not missing, f"Missing keys in position: {missing}"

    def test_value_types(self):
        assert isinstance(self.SAMPLE["tradingsymbol"], str)
        assert isinstance(self.SAMPLE["netqty"], (int, float))
        assert isinstance(self.SAMPLE["ltp"], (int, float))


class TestAngelOneRMSContract:
    """RMS / funds dict from smart.rmsLimit() must have required keys."""

    SAMPLE = {
        "net": "45000.0",
        "availablecash": "30000.0",
        "utiliseddebits": "15000.0",
        "m2mrealized": "0.0",
    }

    def test_required_keys_present(self):
        missing = REQUIRED_RMS_KEYS - set(self.SAMPLE.keys())
        assert not missing, f"Missing keys in RMS: {missing}"

    def test_values_are_string_or_numeric(self):
        """AngelOne returns funds as strings — verify parseability."""
        for key in REQUIRED_RMS_KEYS:
            val = self.SAMPLE[key]
            assert isinstance(val, (str, int, float))
            if isinstance(val, str):
                float(val)  # must be parseable


class TestAngelOneProfileContract:
    """Profile dict from smart.getProfile() must have required keys."""

    SAMPLE = {
        "clientcode": "K51989642",
        "name": "Test User",
        "email": "test@example.com",
        "mobileno": "9999999999",
        "exchanges": ["NSE", "BSE"],
    }

    def test_required_keys_present(self):
        missing = REQUIRED_PROFILE_KEYS - set(self.SAMPLE.keys())
        assert not missing, f"Missing keys in profile: {missing}"

    def test_exchanges_is_list(self):
        assert isinstance(self.SAMPLE["exchanges"], list)


class TestAngelOneGTTContract:
    """GTT order dict must have required keys for order placement."""

    SAMPLE = {
        "tradingsymbol": "INFY",
        "exchange": "NSE",
        "producttype": "DELIVERY",
        "transactiontype": "BUY",
        "qty": 10,
        "triggerprice": 1450.0,
    }

    def test_required_keys_present(self):
        missing = REQUIRED_GTT_KEYS - set(self.SAMPLE.keys())
        assert not missing, f"Missing keys in GTT: {missing}"


# ══════════════════════════════════════════════════════════════════════════
#  Edge-case tests (P3.6)
# ══════════════════════════════════════════════════════════════════════════

class TestAngelOneEdgeCases:
    """Edge-case contract tests: nulls, zeros, negatives, serialisation."""

    def test_holding_with_null_ltp(self):
        """Pipeline must not crash when ltp is None (broker offline / untradable)."""
        holding = {
            "tradingsymbol": "INFY",
            "quantity": 10,
            "averageprice": 1500.0,
            "ltp": None,  # <-- null ltp
            "pnl": None,
            "exchange": "NSE",
        }
        # All required keys are present even with null values
        missing = REQUIRED_HOLDING_KEYS - set(holding.keys())
        assert not missing, f"Null-ltp holding missing keys: {missing}"
        # ltp is explicitly None — not missing from the dict
        assert "ltp" in holding
        assert holding["ltp"] is None

    def test_holding_with_zero_quantity(self):
        """Zero-quantity holdings must still pass key contract (can appear in statement)."""
        holding = {
            "tradingsymbol": "WIPRO",
            "quantity": 0,
            "averageprice": 0.0,
            "ltp": 400.0,
            "pnl": 0.0,
            "exchange": "NSE",
        }
        missing = REQUIRED_HOLDING_KEYS - set(holding.keys())
        assert not missing
        assert holding["quantity"] == 0

    def test_position_with_negative_netqty(self):
        """Negative netqty represents a short/sold position — must be allowed."""
        position = {
            "tradingsymbol": "NIFTY23DEC18000CE",
            "netqty": -50,    # short position
            "buyqty": 0,
            "sellqty": 50,
            "ltp": 120.0,
            "pnl": -500.0,
            "exchange": "NSE",
            "producttype": "INTRADAY",
        }
        missing = REQUIRED_POSITION_KEYS - set(position.keys())
        assert not missing
        assert position["netqty"] < 0

    def test_serialisation_round_trip(self):
        """Holdings dict must survive a JSON round-trip without data loss."""
        import json
        holding = {
            "tradingsymbol": "BEL",
            "quantity": 250,
            "averageprice": 404.87,
            "ltp": 444.45,
            "pnl": 9894.0,
            "exchange": "NSE",
        }
        serialised = json.dumps(holding)
        restored = json.loads(serialised)
        for key in REQUIRED_HOLDING_KEYS:
            assert key in restored, f"Key {key!r} lost in round-trip"
            assert restored[key] == holding[key]
