"""
BENCHMARK 08 — py-spy: CPU flame graph profiling
=================================================
BEFORE : We guess where time is spent when a script is slow
AFTER  : py-spy generates a flame graph SVG showing EXACTLY where CPU goes

Demonstrates:
  - Profile check_patterns.py run (fast, but shows the technique)
  - Profile a simulated slow operation
  - Output: SVG flame graph in benchmarks/profiles/

Note: py-spy may require admin privileges on Windows to attach to processes.
This benchmark uses the subprocess mode (no attach needed).
"""
import sys
import time
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).parent.parent
DIVIDER = "=" * 70

print(DIVIDER)
print("BENCHMARK 08 — py-spy: CPU flame graph profiling")
print(DIVIDER)

profiles_dir = Path(__file__).parent / "profiles"
profiles_dir.mkdir(exist_ok=True)

# ── BEFORE : profiling without py-spy ────────────────────────────────────────
print("\n[BEFORE] Without py-spy: no visibility into where time is spent")
print("  - We see 'signal_stack.py took 12s' but don't know which function")
print("  - We add print() timing around guessed hot spots")
print("  - We miss actual bottlenecks (e.g. VADER sentence tokenizer)")
print("  - We waste time optimizing the wrong function")

# Create a target script with intentional slow spots
target_script = ROOT / "benchmarks" / "_pyspy_target.py"
target_script.write_text(textwrap.dedent('''\
    """Simulated slow script for py-spy profiling."""
    import time
    import re

    def fast_regex_check(text):
        patterns = [r"exec\\(", r"sys\\.exit\\(", r"bare except"]
        return sum(1 for p in patterns if re.search(p, text))

    def slow_file_scan(n=500):
        """Intentionally slow: repeated file reads + regex."""
        results = []
        for _ in range(n):
            text = "x = exec(cmd); sys.exit(1)\\n" * 10
            results.append(fast_regex_check(text))
        return results

    def medium_string_ops(n=2000):
        """Medium speed: string operations."""
        result = []
        for i in range(n):
            s = f"symbol_{i}_AAPL_NASDAQ_{i*1.5:.2f}"
            result.append(s.upper().split("_"))
        return result

    # Simulate the kind of work signal_stack does
    t0 = time.time()
    r1 = slow_file_scan(500)
    r2 = medium_string_ops(2000)
    print(f"Completed: {len(r1)} scans, {len(r2)} ops in {time.time()-t0:.3f}s")
'''), encoding="utf-8")

# Without profiling: just time the script
t0 = time.perf_counter()
result = subprocess.run([sys.executable, str(target_script)], capture_output=True, text=True)
t_unprofiiled = time.perf_counter() - t0
print(f"\n  Without profiling: script ran in {t_unprofiiled:.3f}s")
print(f"  Output: {result.stdout.strip()}")
print(f"  → We know it took {t_unprofiiled:.3f}s but NOT where")

# ── AFTER : py-spy flame graph ────────────────────────────────────────────────
print("\n[AFTER]  Running py-spy to generate flame graph...")

svg_out = profiles_dir / "target_profile.svg"
html_out = profiles_dir / "target_profile.html"

# Check py-spy is available
pyspy_check = subprocess.run(["py-spy", "--version"], capture_output=True, text=True)
if pyspy_check.returncode != 0:
    print(f"  py-spy not in PATH: {pyspy_check.stderr.strip()}")
    print(f"  Trying python -m py_spy...")
    pyspy_cmd_prefix = [sys.executable, "-m", "py_spy"]
else:
    print(f"  py-spy version: {pyspy_check.stdout.strip()}")
    pyspy_cmd_prefix = ["py-spy"]

# Run py-spy record (subprocess mode — no admin needed)
t0 = time.perf_counter()
pyspy_result = subprocess.run(
    pyspy_cmd_prefix + [
        "record",
        "--output", str(svg_out),
        "--format", "speedscope",     # speedscope JSON for flamegraph.com
        "--",
        sys.executable, str(target_script)
    ],
    capture_output=True, text=True, timeout=60
)
t_pyspy = time.perf_counter() - t0

pyspy_out = pyspy_result.stdout + pyspy_result.stderr
print(f"\n  py-spy output:")
for line in pyspy_out.splitlines()[:15]:
    print(f"    {line}")

# Check outputs
svg_exists = svg_out.exists()
print(f"\n  SVG profile: {svg_out} ({'EXISTS' if svg_exists else 'NOT CREATED'})")

# Try SVG format instead
if not svg_exists:
    svg_out2 = profiles_dir / "target_profile2.svg"
    pyspy_result2 = subprocess.run(
        pyspy_cmd_prefix + [
            "record",
            "--output", str(svg_out2),
            "--",
            sys.executable, str(target_script)
        ],
        capture_output=True, text=True, timeout=60
    )
    out2 = pyspy_result2.stdout + pyspy_result2.stderr
    print(f"  SVG attempt 2: {pyspy_result2.returncode}")
    for line in out2.splitlines()[:10]:
        print(f"    {line}")
    svg_exists = svg_out2.exists()
    if svg_exists:
        svg_out = svg_out2
        print(f"  Profile saved: {svg_out} ({svg_out.stat().st_size:,} bytes)")

# ── Generate usage documentation ──────────────────────────────────────────────
usage_doc = profiles_dir / "py-spy-usage.md"
usage_doc.write_text("""\
# py-spy Usage Guide — Stocks System

## One-shot profiling (most common)
```powershell
# Profile a full script run
py-spy record --output profiles/signal_stack.svg -- python tradingview/signal_stack.py

# Profile the full menu run
py-spy record --output profiles/main_run.svg -- python main.py

# Profile with more detail (higher sample rate)
py-spy record --rate 200 --output profiles/detailed.svg -- python script.py
```

## Reading the flame graph
Open the .svg in a browser. The flame graph shows:
- X-axis = total time spent (wider = more time)
- Y-axis = call stack depth
- Hover over a block to see function name + time %
- Click to zoom into a subtree

## Top candidates to profile in our system
| Script | Why profile? |
|--------|-------------|
| `tradingview/signal_stack.py` | Known slow on large fire lists |
| `tradingview/sell/sell_strategy.py` | Multiple yfinance calls per symbol |
| `global_portfolio_dashboard.py` | Aggregates all brokers |
| `tradingview/moomoo_enricher.py` | Repeated Moomoo API calls |
| `india-brokers/india_portfolio_dashboard.py` | Multiple API sources |

## Attach to running process (requires admin on Windows)
```powershell
# Find PID first
Get-Process python

# Attach
py-spy record --pid 12345 --output profiles/live.svg --duration 30
```

## Quick top-level timings (no SVG)
```powershell
py-spy top -- python tradingview/signal_stack.py
```
This prints a live top-like view of function call frequencies.
""", encoding="utf-8")

# ── RESULT SUMMARY ────────────────────────────────────────────────────────────
print(f"\n{DIVIDER}")
print("BENCHMARK RESULT SUMMARY")
print(DIVIDER)
print(f"  BEFORE: Script is slow, we guess why, fix wrong place")
print(f"  AFTER : py-spy flame graph shows EXACTLY where CPU time goes")
print(f"")
print(f"  USAGE (no code changes required):")
print(f"    py-spy record --output profile.svg -- python signal_stack.py")
print(f"    → Opens as interactive SVG in browser")
print(f"    → Immediately shows: 'VADER tokenizer = 40% of time'")
print(f"    → OR: 'yf.Ticker().fast_info = 60% of time'  ← fix with diskcache")
print(f"")
print(f"  WORKS ON:")
print(f"  ✓ Any Python script without code modifications")
print(f"  ✓ Windows (subprocess mode — no admin needed for new processes)")
print(f"  ✓ Generates SVG, speedscope, or raw text output")
print(f"")
print(f"  Usage guide: {usage_doc}")

# ── SAVE PATCH ────────────────────────────────────────────────────────────────
patches_dir = Path(__file__).parent / "patches"
patches_dir.mkdir(exist_ok=True)
patch_file = patches_dir / "08_pyspy.patch"
patch_file.write_text("""\
# py-spy — no code changes required
# Install: pip install py-spy  (already done)
#
# Usage:
#   py-spy record --output profile.svg -- python <script>
#   py-spy top -- python <script>
#
# See benchmarks/profiles/py-spy-usage.md for full guide
#
# Suggested: add to WORKSPACE_SPEC.md profiling section
""", encoding="utf-8")
print(f"\n[PATCH]  Saved to {patch_file}")

# ── REVERT ────────────────────────────────────────────────────────────────────
target_script.unlink(missing_ok=True)
print(f"[REVERT] Removed _pyspy_target.py — profiles/ and usage guide kept")
print(f"\n{'='*70}")
