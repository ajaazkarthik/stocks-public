"""
BENCHMARK 08 — Sequential vs Parallel broker fetch
====================================================
BEFORE : Sequential fetch (current global_portfolio_dashboard.py main())
AFTER  : Parallel fetch (ThreadPoolExecutor, max_workers=5)

Simulates realistic broker response times based on observed production timing:
  - Moomoo AU  (OpenD socket):     1.5 s
  - Moomoo SG  (OpenD socket):     1.5 s
  - IBKR       (Flex Query HTTP):  3.0 s  / 0.1 s if cached
  - Webull AU  (PDF + yfinance):   0.1 s  / 2.0 s if not cached
  - India      (Zerodha + AO):     4.0 s  (2 API calls + TOTP)

Sequential total (all live):  ~10-12 s
Parallel total  (all live):   ~4.0 s  (bottleneck = India)
Expected speedup:              2.5-3x
"""
import time
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait

ROOT = Path(__file__).parent.parent
DIVIDER = "=" * 70

# ── Realistic mock delays (seconds) for each broker ----------------------------
BROKER_DELAYS = {
    "Moomoo-AU":  1.5,   # OpenD socket roundtrip
    "Moomoo-SG":  1.5,   # OpenD socket roundtrip
    "IBKR":       3.0,   # Flex Query HTTP (worst case, no cache)
    "Webull AU":  2.0,   # PDF re-parse + yfinance (cache miss)
    "India":      4.0,   # Zerodha API + AngelOne SmartAPI + TOTP
}

def _mock_fetch_moomoo(label: str, port: int) -> tuple[list[dict], str]:
    """Simulates _fetch_moomoo with realistic I/O delay."""
    time.sleep(BROKER_DELAYS[f"Moomoo-{label}"])
    return [{"symbol": f"AAPL_{label}", "broker": f"Moomoo-{label}"}], \
           f"Live — 12 positions [sim:{BROKER_DELAYS[f'Moomoo-{label}']}s]"

def _mock_fetch_ibkr(skip: bool = False) -> tuple[list[dict], str]:
    """Simulates _fetch_ibkr Flex Query."""
    if skip:
        return [], "Skipped"
    time.sleep(BROKER_DELAYS["IBKR"])
    return [{"symbol": "TSLA", "broker": "IBKR"}], \
           f"Live — 5 positions [sim:{BROKER_DELAYS['IBKR']}s]"

def _mock_fetch_webull_pdf() -> tuple[list[dict], str]:
    """Simulates _fetch_webull_pdf with cache miss (worst case)."""
    time.sleep(BROKER_DELAYS["Webull AU"])
    return [{"symbol": "CBA", "broker": "Webull AU"}], \
           f"PDF — 8 positions [sim:{BROKER_DELAYS['Webull AU']}s]"

def _mock_fetch_india() -> tuple[list[dict], list[dict], str, str]:
    """Simulates _fetch_india (Zerodha + AngelOne sequentially)."""
    time.sleep(BROKER_DELAYS["India"])
    z_pos = [{"symbol": "RELIANCE", "broker": "Zerodha"}]
    a_pos = [{"symbol": "INFY",     "broker": "AngelOne"}]
    t = BROKER_DELAYS["India"]
    return z_pos, a_pos, f"Live — 15 positions [sim:{t}s]", f"Live — 10 positions"

# ── BEFORE: Sequential fetch (current implementation) -------------------------

def run_sequential(no_ibkr: bool = False) -> tuple[float, dict]:
    """Mirror of the current main() sequential fetch block."""
    all_positions = []
    statuses      = {}
    t0 = time.perf_counter()

    au_pos, au_status = _mock_fetch_moomoo("AU", 11111)
    all_positions.extend(au_pos);   statuses["Moomoo-AU"] = au_status

    sg_pos, sg_status = _mock_fetch_moomoo("SG", 11112)
    all_positions.extend(sg_pos);   statuses["Moomoo-SG"] = sg_status

    ib_pos, ib_status = _mock_fetch_ibkr(skip=no_ibkr)
    all_positions.extend(ib_pos);   statuses["IBKR"]      = ib_status

    wb_pos, wb_status = _mock_fetch_webull_pdf()
    all_positions.extend(wb_pos);   statuses["Webull AU"] = wb_status

    z_pos, a_pos, z_status, a_status = _mock_fetch_india()
    all_positions.extend(z_pos + a_pos)
    statuses["Zerodha"]  = z_status
    statuses["AngelOne"] = a_status

    elapsed = time.perf_counter() - t0
    return elapsed, statuses

# ── AFTER: Parallel fetch (proposed implementation) ---------------------------

def run_parallel(no_ibkr: bool = False) -> tuple[float, dict]:
    """Mirror of the proposed parallel fetch block."""
    all_positions = []
    statuses      = {}
    t0 = time.perf_counter()

    with ThreadPoolExecutor(max_workers=5, thread_name_prefix="broker") as pool:
        fut_mau  = pool.submit(_mock_fetch_moomoo, "AU", 11111)
        fut_msg  = pool.submit(_mock_fetch_moomoo, "SG", 11112)
        fut_ibkr = pool.submit(_mock_fetch_ibkr, no_ibkr)
        fut_wb   = pool.submit(_mock_fetch_webull_pdf)
        fut_ind  = pool.submit(_mock_fetch_india)

        all_futs = [fut_mau, fut_msg, fut_ibkr, fut_wb, fut_ind]
        done, not_done = futures_wait(all_futs, timeout=90)
        for f in not_done:
            f.cancel()

    # Collect results safely
    try:
        au_pos, au_status = fut_mau.result(timeout=0)
    except Exception as e:
        au_pos, au_status = [], f"Error: {e}"

    try:
        sg_pos, sg_status = fut_msg.result(timeout=0)
    except Exception as e:
        sg_pos, sg_status = [], f"Error: {e}"

    try:
        ib_pos, ib_status = fut_ibkr.result(timeout=0)
    except Exception as e:
        ib_pos, ib_status = [], f"Error: {e}"

    try:
        wb_pos, wb_status = fut_wb.result(timeout=0)
    except Exception as e:
        wb_pos, wb_status = [], f"Error: {e}"

    try:
        z_pos, a_pos, z_status, a_status = fut_ind.result(timeout=0)
    except Exception as e:
        z_pos, a_pos, z_status, a_status = [], [], f"Error: {e}", "Not attempted"

    all_positions.extend(au_pos + sg_pos + ib_pos + wb_pos + z_pos + a_pos)
    statuses["Moomoo-AU"] = au_status
    statuses["Moomoo-SG"] = sg_status
    statuses["IBKR"]      = ib_status
    statuses["Webull AU"] = wb_status
    statuses["Zerodha"]   = z_status
    statuses["AngelOne"]  = a_status

    elapsed = time.perf_counter() - t0
    return elapsed, statuses

# ── Main -----------------------------------------------------------------------

if __name__ == "__main__":
    print(DIVIDER)
    print("BENCHMARK 08 — Sequential vs Parallel broker fetch")
    print(DIVIDER)
    print()
    print("Simulated broker delays:")
    for k, v in BROKER_DELAYS.items():
        print(f"  {k:<14}: {v:.1f}s")
    print()
    print(f"  Sequential total (sum):  {sum(BROKER_DELAYS.values()):.1f}s (theoretical)")
    print(f"  Parallel total   (max):  {max(BROKER_DELAYS.values()):.1f}s (theoretical)")
    print()

    # Run BEFORE (sequential) — 3 trials
    print("[BEFORE] Sequential fetch — 3 trials")
    seq_times = []
    for i in range(3):
        t, st = run_sequential()
        seq_times.append(t)
        print(f"  Trial {i+1}: {t:.2f}s")
    seq_avg = sum(seq_times) / len(seq_times)
    print(f"  Average: {seq_avg:.2f}s")
    print()

    # Run AFTER (parallel) — 3 trials
    print("[AFTER] Parallel fetch (ThreadPoolExecutor, max_workers=5) — 3 trials")
    par_times = []
    for i in range(3):
        t, st = run_parallel()
        par_times.append(t)
        print(f"  Trial {i+1}: {t:.2f}s")
    par_avg = sum(par_times) / len(par_times)
    print(f"  Average: {par_avg:.2f}s")
    print()

    # Summary
    speedup    = seq_avg / par_avg
    saved_s    = seq_avg - par_avg
    print(DIVIDER)
    print("RESULT")
    print(DIVIDER)
    print(f"  Sequential avg : {seq_avg:.2f}s")
    print(f"  Parallel avg   : {par_avg:.2f}s")
    print(f"  Time saved     : {saved_s:.2f}s per dashboard run")
    print(f"  Speedup        : {speedup:.1f}x")
    print()
    if speedup >= 2.0:
        print("  VERDICT: ✓ MAJOR IMPROVEMENT — implement this change")
        print(f"  Every dashboard run saves ~{saved_s:.0f}s")
        print(f"  10 runs/day = {saved_s * 10:.0f}s saved daily")
    elif speedup >= 1.5:
        print("  VERDICT: MODERATE improvement — worth it if brokers are reliable")
    else:
        print("  VERDICT: ✗ MARGINAL — skip this change")
    print()

    # Show that statuses are identical
    _, seq_st = run_sequential()
    _, par_st = run_parallel()
    print("  Result integrity check:")
    for broker in ["Moomoo-AU", "Moomoo-SG", "IBKR", "Webull AU", "Zerodha", "AngelOne"]:
        ok = "✓" if seq_st.get(broker) == par_st.get(broker) else "✗ MISMATCH"
        print(f"    {broker:<14}: {ok}")
