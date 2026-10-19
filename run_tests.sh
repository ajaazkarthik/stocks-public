#!/usr/bin/env bash
# run_tests.sh — Full CI Gate for stocks trading system
# =====================================================================
# Run this before ANY commit to verify all tests pass.
# Expected: 680+ tests across all test files.
#
# Usage:
#   ./run_tests.sh          # run all tests
#   ./run_tests.sh --quick  # run only the core gate (fast)
# =====================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

QUICK=false
for arg in "$@"; do
    case "$arg" in
        --quick|-q) QUICK=true ;;
    esac
done

if $QUICK; then
    echo ""
    echo "=== QUICK GATE (core tests only) ==="
    python -m pytest tests/test_menu_handlers.py tests/test_html_reports.py -q --tb=short
else
    echo ""
    echo "=== FULL CI GATE ==="
    python -m pytest \
        tests/test_menu_handlers.py \
        tests/test_html_reports.py \
        tests/test_ibkr_client_portal.py \
        india-brokers/test_india_market_internals.py \
        tradingview/signal_engine/tests/test_moomoo_enricher.py \
        tests/contracts/ \
        --cov=tradingview --cov=india-brokers \
        --cov-fail-under=8 \
        --cov-report=term-missing:skip-covered \
        -q --tb=short
fi

exit $?
