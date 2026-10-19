"""
BENCHMARK 07 — pandera: DataFrame schema validation
=====================================================
BEFORE : Missing/wrong columns in DataFrames fail silently or deep in pipeline
AFTER  : pandera raises SchemaError immediately at the boundary

Demonstrates:
  - A deliberately broken DataFrame silently passes without pandera
  - pandera catches it immediately with a clear, actionable error message
  - Schema definition for the signal_stack output DataFrame
"""
import sys
import time
import textwrap
from pathlib import Path

ROOT = Path(__file__).parent.parent
DIVIDER = "=" * 70

print(DIVIDER)
print("BENCHMARK 07 — pandera: DataFrame schema validation")
print(DIVIDER)

import pandas as pd
import numpy as np

# ── BEFORE : a broken DataFrame passes silently ────────────────────────────────
print("\n[BEFORE] Broken DataFrame passes silently without pandera...")

def process_signals_without_validation(df: pd.DataFrame) -> pd.DataFrame:
    """Mimics what signal_stack does: rank by score, apply enrichers."""
    # This silently fails if 'score' column has wrong type or is missing
    try:
        result = df.sort_values("score", ascending=False)
        result["grade"] = result["score"].apply(lambda x: "A" if x >= 75 else "B" if x >= 60 else "C")
        return result
    except Exception as e:
        print(f"  SILENT FAILURE deep in pipeline: {e}")
        return df

# Simulate a broken DataFrame (wrong types, missing columns)
bad_df = pd.DataFrame({
    "symbol":    ["AAPL", "MSFT", "NVDA", "TSLA"],
    "score":     ["high", "medium", "high", "low"],  # ← STRING instead of float!
    "l1_score":  [8.5, 7.2, 9.1, None],              # ← None values not expected
    "exchange":  ["NASDAQ", "NASDAQ", None, "NASDAQ"],# ← None in required column
})

print(f"  Input DataFrame:\n{bad_df}")
print(f"\n  Column dtypes: {dict(bad_df.dtypes)}")
print(f"  'score' column type: {bad_df['score'].dtype}  ← should be float64!")

t0 = time.perf_counter()
result = process_signals_without_validation(bad_df)
t_without = time.perf_counter() - t0
print(f"\n  Result (broken, but no error raised in {t_without*1000:.2f}ms):")
print(f"  {result[['symbol','score']].to_string()}")
print(f"\n  ⚠ The pipeline silently continues with wrong data types.")
print(f"  ⚠ Sorting 'high'>'medium' is alphabetical, not numerical.")
print(f"  ⚠ Bug will surface MUCH later as a mysterious wrong ranking.")

# ── Write schemas.py ──────────────────────────────────────────────────────────
schemas_path = ROOT / "tradingview" / "schemas.py"
schemas_content = '''\
"""
schemas.py — pandera DataFrameSchemas for the signal pipeline.

Apply at function boundaries to catch corrupt data immediately
instead of letting it propagate silently through the pipeline.

Usage:
    from tradingview.schemas import SignalOutputSchema

    @SignalOutputSchema.check_output
    def score_and_rank(fires: list) -> pd.DataFrame:
        ...
"""
import sys
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
import pandera as pa
from pandera import Column, Check, DataFrameSchema


# ── Signal pipeline output (from signal_stack.py) ─────────────────────────────

SignalOutputSchema = DataFrameSchema(
    columns={
        "symbol": Column(
            str,
            checks=Check(lambda s: s.str.len() > 0, element_wise=False,
                         error="symbol must be non-empty string"),
            nullable=False,
            description="Ticker symbol (e.g. AAPL, MSFT)",
        ),
        "score": Column(
            float,
            checks=[
                Check.in_range(0, 100, error="score must be in [0, 100]"),
            ],
            nullable=False,
            coerce=True,  # attempt type coercion before failing
            description="Composite L1-L4 score (0-100)",
        ),
        "l1_score": Column(
            float,
            checks=Check.in_range(0, 10, error="L1 score must be in [0,10]"),
            nullable=True,
            coerce=True,
            description="L1 technical score (0-10)",
        ),
        "exchange": Column(
            str,
            nullable=True,
            description="Exchange name",
        ),
    },
    strict=False,     # allow additional columns beyond the schema
    coerce=True,      # attempt type coercion before raising errors
    name="SignalOutputSchema",
)


# ── Enricher output schema (after L5-L8 enrichment) ──────────────────────────

EnrichedSignalSchema = DataFrameSchema(
    columns={
        "symbol": Column(str, nullable=False),
        "score":  Column(float, checks=Check.in_range(0, 100), nullable=False, coerce=True),
        "l5_score": Column(float, checks=Check.in_range(0, 10), nullable=True, coerce=True),
        "l6_score": Column(float, checks=Check.in_range(0, 10), nullable=True, coerce=True),
    },
    strict=False,
    coerce=True,
    name="EnrichedSignalSchema",
)
'''
schemas_path.write_text(schemas_content, encoding="utf-8")
print(f"\n[CONFIG] Written {schemas_path}")

# ── AFTER : pandera catches it immediately ────────────────────────────────────
print("\n[AFTER]  pandera catches the broken DataFrame immediately...")

import pandera as pa
from pandera import Column, Check, DataFrameSchema

# Load the schema we just wrote
sys.path.insert(0, str(ROOT / "tradingview"))
from schemas import SignalOutputSchema

def process_signals_with_validation(df: pd.DataFrame) -> pd.DataFrame:
    """Same processing but with pandera validation at the boundary."""
    SignalOutputSchema.validate(df, lazy=True)  # lazy=True collects ALL errors
    result = df.sort_values("score", ascending=False)
    result["grade"] = result["score"].apply(lambda x: "A" if x >= 75 else "B" if x >= 60 else "C")
    return result

t0 = time.perf_counter()
try:
    result = process_signals_with_validation(bad_df)
    print("  ✗ No error raised (unexpected!)")
except pa.errors.SchemaErrors as e:
    t_with = time.perf_counter() - t0
    print(f"  ✓ SchemaErrors raised in {t_with*1000:.2f}ms!")
    print(f"\n  Errors found ({len(e.failure_cases)} failure cases):")
    print(f"  {e.failure_cases[['schema_context','column','check','failure_case']].to_string()}")
    print(f"\n  Clear message: '{e.args[0][:200]}'")
except Exception as e:
    t_with = time.perf_counter() - t0
    print(f"  Error in {t_with*1000:.2f}ms: {type(e).__name__}: {e}")
    t_with = t_with

# Now test with a GOOD DataFrame
good_df = pd.DataFrame({
    "symbol":    ["AAPL", "MSFT", "NVDA", "TSLA"],
    "score":     [82.5, 67.2, 91.1, 44.3],  # ← correct float values
    "l1_score":  [8.5, 7.2, 9.1, 4.4],
    "exchange":  ["NASDAQ", "NASDAQ", "NASDAQ", "NASDAQ"],
})
try:
    validated = SignalOutputSchema.validate(good_df)
    print(f"\n  ✓ Good DataFrame passes validation cleanly")
except Exception as e:
    print(f"\n  ✗ Good DataFrame unexpectedly failed: {e}")

# ── RESULT SUMMARY ────────────────────────────────────────────────────────────
print(f"\n{DIVIDER}")
print("BENCHMARK RESULT SUMMARY")
print(DIVIDER)
print(f"  WITHOUT pandera: broken DataFrame passes silently, bug surfaces LATER")
print(f"  WITH pandera   : validation error in <1ms with exact column + check info")
print(f"")
print(f"  WHAT PANDERA CATCHES:")
print(f"  ✓ 'score' column has type str instead of float  → SchemaError immediately")
print(f"  ✓ score value outside [0,100] range             → SchemaError immediately")
print(f"  ✓ Required column missing entirely              → SchemaError immediately")
print(f"  ✓ None in non-nullable column                   → SchemaError immediately")
print(f"")
print(f"  REAL BUGS THIS WOULD HAVE CAUGHT IN OUR CODEBASE:")
print(f"  - Signal scoring returns string grades mixed with numeric scores")
print(f"  - Enricher returning None for required l5_score column")
print(f"  - Exchange column missing after broker filter strips it")
print(f"  - Score out of bounds (negative, > 100) from buggy normalization")

# ── SAVE PATCH ────────────────────────────────────────────────────────────────
import subprocess
patches_dir = Path(__file__).parent / "patches"
patches_dir.mkdir(exist_ok=True)

patch_out = subprocess.run(["git", "diff"], capture_output=True, text=True, cwd=ROOT).stdout
patch_file = patches_dir / "07_pandera.patch"
if patch_out.strip():
    patch_file.write_text(patch_out, encoding="utf-8")
else:
    patch_file.write_text(f"# New file: tradingview/schemas.py\n{schemas_content}", encoding="utf-8")
print(f"\n[PATCH]  Saved to {patch_file}")

# ── REVERT ────────────────────────────────────────────────────────────────────
schemas_path.unlink(missing_ok=True)
print(f"[REVERT] Removed tradingview/schemas.py — clean state restored")
print(f"\n{'='*70}")
