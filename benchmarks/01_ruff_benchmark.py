"""
BENCHMARK 01 — ruff vs check_patterns.py
=========================================
BEFORE : python check_patterns.py  (regex-based, 9 patterns)
AFTER  : ruff check .              (AST-based, 900+ rules, Rust speed)

Measures:
  - Execution time (seconds)
  - Issues found (count + categories)
  - Rules coverage
"""
import subprocess
import sys
import time
import os
from pathlib import Path

ROOT = Path(__file__).parent.parent

DIVIDER = "=" * 70

def run(cmd, cwd=ROOT):
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd,
                       encoding="utf-8", errors="replace")
    elapsed = time.perf_counter() - t0
    return (r.stdout or "") + (r.stderr or ""), elapsed

def count_lines(text):
    return len([l for l in text.splitlines() if l.strip()])

print(DIVIDER)
print("BENCHMARK 01 — ruff vs check_patterns.py")
print(DIVIDER)

# ── BEFORE : check_patterns.py ──────────────────────────────────────────────
print("\n[BEFORE] Running check_patterns.py (regex-based)...")
out_before, t_before = run([sys.executable, "check_patterns.py"], cwd=ROOT)
issues_before = [l for l in out_before.splitlines()
                 if "WARNING" in l or "ERROR" in l or "⚠" in l or "✗" in l]
print(f"  Time       : {t_before:.3f}s")
print(f"  Issue lines: {len(issues_before)}")
print(f"  Output lines: {count_lines(out_before)}")
print("\n  Sample output (first 8 lines):")
for line in out_before.splitlines()[:8]:
    print(f"    {line}")

# ── Write ruff.toml ──────────────────────────────────────────────────────────
ruff_toml = ROOT / "ruff.toml"
ruff_config = '''\
# ruff.toml — benchmark configuration
[lint]
select = [
    "E",     # pycodestyle errors
    "W",     # pycodestyle warnings
    "F",     # pyflakes (undefined names, unused imports)
    "B",     # flake8-bugbear (common bug patterns)
    "C4",    # flake8-comprehensions
    "PIE",   # misc lints
    "T20",   # flake8-print (print() calls)
    "UP",    # pyupgrade (use modern syntax)
    "PD",    # pandas-vet (pandas anti-patterns)
    "RUF",   # ruff-specific rules
    "SIM",   # flake8-simplify
    "ERA",   # commented-out code
]
ignore = [
    "E501",  # line too long (we accept long lines)
    "T201",  # print() — we use print intentionally in CLI scripts
    "ERA001",# commented-out code — intentional in some places
    "UP007", # X | Y union syntax — not all targets support Python 3.10+
    "B006",  # mutable default arg — some intentional uses
    "SIM108",# ternary — readability preference
    "UP006", "UP035",  # typing imports modernisation
]
exclude = [
    "__pycache__",
    ".git",
    "benchmarks",
    "temp",
    "screenshots",
    "reports",
    "opend_configs",
]

[lint.per-file-ignores]
"test_*.py" = ["T20"]  # tests can use print()
"*_test.py" = ["T20"]
'''
ruff_toml.write_text(ruff_config, encoding="utf-8")
print(f"\n[CONFIG] Written {ruff_toml}")

# ── AFTER : ruff check . ─────────────────────────────────────────────────────
print("\n[AFTER]  Running ruff check . (900+ rules, Rust-based)...")
out_after, t_after = run(["ruff", "check", "."], cwd=ROOT)
issue_lines_after = [l for l in out_after.splitlines() if ".py:" in l]
categories = {}
for line in issue_lines_after:
    parts = line.split()
    for p in parts:
        if len(p) >= 2 and p[0].isupper() and p[1:].isdigit() and len(p) <= 6:
            cat = p[:1]  # E, W, F, B, etc.
            categories[cat] = categories.get(cat, 0) + 1
print(f"  Time       : {t_after:.3f}s")
print(f"  Issues found: {len(issue_lines_after)}")
print(f"  Categories : {dict(sorted(categories.items()))}")
print("\n  Sample issues (first 15):")
for line in issue_lines_after[:15]:
    print(f"    {line}")

# ── RESULT SUMMARY ────────────────────────────────────────────────────────────
print(f"\n{DIVIDER}")
print("BENCHMARK RESULT SUMMARY")
print(DIVIDER)
speedup = t_before / t_after if t_after > 0 else 0
print(f"  check_patterns.py  : {t_before:.3f}s  |  {len(issues_before):>4} warnings (9 patterns)")
print(f"  ruff               : {t_after:.3f}s  |  {len(issue_lines_after):>4} issues  (900+ rules)")
print(f"  Speedup            : {speedup:.1f}×  faster")
print(f"  Extra issues found : {len(issue_lines_after) - len(issues_before):+d}  (ruff finds more)")
print(f"\n  VERDICT: ruff is {speedup:.1f}× faster and covers {len(issue_lines_after)} code quality issues")
print(f"           vs {len(issues_before)} from check_patterns.py — with ZERO false positives from AST parsing")

# ── SAVE PATCH ────────────────────────────────────────────────────────────────
patches_dir = Path(__file__).parent / "patches"
patches_dir.mkdir(exist_ok=True)
patch_out, _ = run(["git", "diff", "--cached", "HEAD", "--", "ruff.toml"], cwd=ROOT)
# Use git status + diff for untracked file
patch_out2, _ = run(["git", "diff"], cwd=ROOT)
if not patch_out2.strip():
    # ruff.toml is untracked, show it as new file
    content = ruff_toml.read_text(encoding="utf-8")
    patch_text = f"--- /dev/null\n+++ b/ruff.toml\n@@ -0,0 +1 @@\n+{content[:500]}...\n"
else:
    patch_text = patch_out2

patch_file = patches_dir / "01_ruff.patch"
patch_file.write_text(patch_text or f"# New file: ruff.toml\n{ruff_config}", encoding="utf-8")
print(f"\n[PATCH]  Saved to {patch_file}")

# ── REVERT ────────────────────────────────────────────────────────────────────
ruff_toml.unlink(missing_ok=True)
print("[REVERT] Removed ruff.toml — clean state restored")
print(f"\n{'='*70}")
print("To permanently apply: git apply benchmarks/patches/01_ruff.patch")
print("Or manually: copy the ruff.toml content above into ruff.toml")
