"""
schemas.py — pandera DataFrameSchemas for the signal pipeline.

Apply at function boundaries to catch corrupt data immediately
instead of letting it propagate silently through the pipeline.

Usage:
    from tradingview.schemas import SignalOutputSchema, EnrichedSignalSchema

    # As a decorator (validates return value):
    @SignalOutputSchema.check_output
    def score_and_rank(fires: list) -> pd.DataFrame:
        ...

    # Inline validation (raises pandera.errors.SchemaErrors on failure):
    SignalOutputSchema.validate(df, lazy=True)  # lazy=True = collect ALL errors
"""
import sys

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pandera.pandas import Column, Check, DataFrameSchema


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
