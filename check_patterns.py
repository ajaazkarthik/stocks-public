"""
check_patterns.py — Bad-pattern linter for the stocks trading system.

Scans Python files for known bug patterns documented in
.github/known-bugs-and-pitfalls.md. Returns exit code 1 if any are found.

Usage:
    python check_patterns.py                  # scan all .py files in workspace
    python check_patterns.py path/to/file.py  # scan specific files
    python check_patterns.py --changed        # scan only git-staged/modified .py files

Designed to run as a pre-commit hook or manually before pushing.
"""
import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent

# ---------------------------------------------------------------------------
# Pattern registry — each entry is:
#   (id, regex, description, files_regex, fix_hint)
# files_regex: None = all .py files, otherwise match against the file path
# ---------------------------------------------------------------------------
_PATTERNS = [
    # ── Section G: broker_stats ──────────────────────────────────────────
    (
        "G1",
        re.compile(r"\bbroker_stats\s*(?::\s*dict\s*)?\=\s*\{\s*\}", re.MULTILINE),
        "Empty broker_stats init — offline brokers will silently disappear from PnL table",
        None,
        "Pre-seed: broker_stats = {b: {'count': 0, 'pnl_usd': 0.0, 'cost_usd': 0.0} for b in _KNOWN_BROKERS}",
    ),
    # ── Section A3: optionGreeks (plural) ────────────────────────────────
    (
        "A3",
        re.compile(r"optionGreeks\b"),
        "Wrong Angel One endpoint — use /v1/optionGreek (singular), not optionGreeks (plural)",
        None,
        "Replace with smart.optionGreek(params) — plural endpoint triggers WAF 200 HTML response",
    ),    # GR16 is a file-level check — see _file_check_gr16() below (avoids ex.map() false positives)
    # ── Section A4: double-Bearer header ─────────────────────────────────
    (
        "A4",
        re.compile(r'"Bearer\s+"\s*\+\s*\w*[Tt]oken\b'),
        "Potential double-Bearer header — if token already has 'Bearer ' prefix this produces 'Bearer Bearer eyJ...'",
        None,
        "Strip first: raw_token = token.replace('Bearer ', '').strip(); headers = {'Authorization': f'Bearer {raw_token}'}",
    ),
    # ── Section J: hardcoded API key fallback ────────────────────────────
    (
        "J1",
        re.compile(r'os\.environ\.get\(["\']ZERODHA_API_KEY["\'],\s*["\'][^"\']+["\']'),
        "Hardcoded ZERODHA_API_KEY fallback — prefer os.environ['ZERODHA_API_KEY'] to raise KeyError if unset",
        None,
        "Remove the default fallback value: os.environ['ZERODHA_API_KEY']",
    ),
    (
        "J2",
        re.compile(r'(ANGELONE_TOTP_SECRET|TOTP_SECRET)\s*(?:=\s*["\'][A-Z2-7]{16,}["\']|in\s+["\'][A-Z2-7]{16,}["\'])'),
        "Hardcoded TOTP secret detected — must be loaded from environment variable only",
        None,
        "Use os.environ['ANGELONE_TOTP_SECRET'] — never hardcode secrets",
    ),
    # ── Section I: sqlite :memory: multi-connect ────────────────────────
    (
        "I3",
        re.compile(r'connect\(["\']:memory:["\']'),
        "SQLite :memory: connection — calling this more than once creates a NEW empty DB each time",
        re.compile(r"(test_|_test)"),  # only flag in non-test files if we want; for now flag everywhere
        "Cache the connection: if not hasattr(module, '_conn'): module._conn = sqlite3.connect(':memory:')",
    ),
    # ── Section H: actionable_log path ───────────────────────────────────
    (
        "H2",
        re.compile(r'["\']tradingview[/\\\\]reports[/\\\\]actionable_log\.json["\']'),
        "Wrong actionable_log.json path — old tradingview/reports/ path no longer exists; use TV / 'actionable_log.json' (primary) or 'reports/actionable_log.json' (secondary)",
        None,
        "Use: TV / 'actionable_log.json'",
    ),
    # ── Section K: eyewashing tests ──────────────────────────────────────
    (
        "K1a",
        re.compile(r"assert\s+\w+\s+or\s+True\b"),
        "Weakened assertion — 'assert X or True' always passes regardless of X",
        re.compile(r"test_"),
        "Fix the production code; do not weaken the assertion",
    ),
    (
        "K1b",
        re.compile(r"pytest\.skip\([^)]*invariant|pytest\.skip\([^)]*broker|pytest\.skip\([^)]*coverage"),
        "Suspicious pytest.skip — skipping a coverage/invariant test is likely eyewashing",
        re.compile(r"test_"),
        "Remove the skip and fix the underlying production code",
    ),
    # ── New standalone script without UTF-8 reconfigure ─────────────────
    # ── Section R: Wrong report output path ──────────────────────────────
    (
        "R1",
        re.compile(r'''TV\s*/\s*["']reports["']'''),
        "Wrong report path — TV / 'reports' outputs to tradingview/reports/ instead of stocks/reports/",
        None,
        "Use REPORTS from path_config, or ROOT / 'reports', or .parent.parent / 'reports'",
    ),
    (
        "R2",
        re.compile(r'''(?:ROOT|_ROOT|WORKSPACE|WS)\s*/\s*["']tradingview["']\s*/\s*["']reports["']'''),
        "Wrong report path — ROOT / 'tradingview' / 'reports' outputs to tradingview/reports/ instead of stocks/reports/",
        None,
        "Use REPORTS from path_config (imports as: from path_config import REPORTS)",
    ),
    # (heuristic: if file has if __name__ == '__main__' and print() but no sys.stdout.reconfigure)
    # This is too complex for a regex — handled by AST check below
]

# ---------------------------------------------------------------------------
# AST-based checks (more accurate than regex)
# ---------------------------------------------------------------------------

def _ast_check_utf8_reconfigure(path: Path, source: str) -> list[tuple[int, str, str, str]]:
    """Flag standalone scripts that have print() but lack the UTF-8 reconfigure block."""
    issues = []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return issues

    has_main_guard = False
    has_print = False
    has_reconfigure = False

    for node in ast.walk(tree):
        # Check for if __name__ == "__main__":
        if isinstance(node, ast.If):
            test = node.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == "__main__"
            ):
                has_main_guard = True
        # Check for print() calls at module level
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "print":
                has_print = True
        # Check for sys.stdout.reconfigure
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "reconfigure":
                has_reconfigure = True

    if has_main_guard and has_print and not has_reconfigure:
        # Only flag root-level standalone scripts — not tradingview/ subdirectory test helpers
        if path.suffix == ".py" and "test_" not in path.name and not path.name.startswith("_"):
            issues.append((
                1,
                "UTF8",
                "Standalone script with print() lacks sys.stdout.reconfigure(encoding='utf-8') block",
                "Add the Win32 UTF-8 reconfigure block near top of file (see copilot-instructions.md)",
            ))
    return issues


# ── GR#16 file-level check ─────────────────────────────────────────────────
# Dangerous: 'with ThreadPoolExecutor(...) as ex:' combined with '.result(timeout=)'
#            — the context manager calls shutdown(wait=True) on __exit__, blocking
#              indefinitely even after TimeoutError fires (root cause: 419s Moomoo hang).
# Safe:      'with ThreadPoolExecutor(...) as ex: list(ex.map(...))' — no timeout future.
_GR16_WITH_TPE = re.compile(r"with\s+ThreadPoolExecutor\s*\(")


def _file_check_gr16(path: Path, source: str) -> list[tuple[int, str, str, str]]:
    """Flag 'with ThreadPoolExecutor(' ONLY when '.result(timeout=' appears within
    30 lines — the dangerous combination that blocks shutdown indefinitely.
    Safe ex.map() patterns are NOT flagged."""
    if path.name == "check_patterns.py":
        return []  # self-exemption: description strings contain the pattern literals
    issues: list[tuple[int, str, str, str]] = []
    lines = source.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "# nocheck" in line:
            continue
        if _GR16_WITH_TPE.search(line):
            # Look ahead up to 30 lines for .result(timeout=
            window = "\n".join(lines[i + 1 : min(i + 31, len(lines))])
            if ".result(timeout=" in window or ".result(timeout =" in window:
                issues.append((
                    i + 1,
                    "GR16",
                    "GR#16 violation: 'with ThreadPoolExecutor(' + '.result(timeout=)' "
                    "blocks shutdown(wait=True) after TimeoutError — context manager "
                    "waits indefinitely even after the timeout fires",
                    "Use: _ex = ThreadPoolExecutor(1); fut = _ex.submit(fn); "
                    "fut.result(timeout=N); _ex.shutdown(wait=False)",
                ))
    return issues


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def scan_file(path: Path) -> list[dict]:
    """Return list of {file, line, pattern_id, message, fix} for all hits."""
    hits = []
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return hits

    lines = source.splitlines()

    for pid, regex, desc, files_regex, fix in _PATTERNS:
        # Skip if this pattern only applies to certain files
        if files_regex is not None:
            rel = str(path).replace("\\", "/")
            if not files_regex.search(rel):
                continue  # pattern doesn't apply to this file path
            # Special case I3: :memory: — only warn in non-test files
            if pid == "I3" and "test_" in path.name:
                continue

        for i, line in enumerate(lines, start=1):
            if regex.search(line):
                # Don't flag lines that are comments explaining the bad pattern
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                    continue
                # Allow inline exemption: add  # nocheck  anywhere on the line
                # e.g.  API_KEY = "abc"  # nocheck: J1 — intentional fallback, system dependency
                if "# nocheck" in line:
                    continue
                hits.append({
                    "file": str(path.relative_to(ROOT)),
                    "line": i,
                    "id": pid,
                    "message": desc,
                    "fix": fix,
                    "code": line.rstrip(),
                })

    # AST checks
    for line_no, pid, desc, fix in _ast_check_utf8_reconfigure(path, source):
        hits.append({
            "file": str(path.relative_to(ROOT)),
            "line": line_no,
            "id": pid,
            "message": desc,
            "fix": fix,
            "code": "(see top of file)",
        })

    # GR16 file-level check — dangerous: 'with ThreadPoolExecutor(' + '.result(timeout='
    for line_no, pid, desc, fix in _file_check_gr16(path, source):
        hits.append({
            "file": str(path.relative_to(ROOT)),
            "line": line_no,
            "id": pid,
            "message": desc,
            "fix": fix,
            "code": "(see context at flagged line)",
        })

    return hits


def get_changed_files() -> list[Path]:
    """Return .py files that are staged or modified according to git."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--cached", "HEAD"],
            capture_output=True, text=True, cwd=ROOT
        )
        staged = set(result.stdout.splitlines())
        result2 = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True, text=True, cwd=ROOT
        )
        modified = set(result2.stdout.splitlines())
        all_changed = staged | modified
        return [ROOT / f for f in all_changed if f.endswith(".py") and (ROOT / f).is_file()]
    except Exception:
        return []


def collect_files(targets: list[str], changed_only: bool) -> list[Path]:
    """Resolve file targets to a list of Path objects."""
    if changed_only:
        return get_changed_files()

    if targets:
        paths = []
        for t in targets:
            p = Path(t)
            if not p.is_absolute():
                p = ROOT / p
            if p.is_dir():
                paths.extend(p.rglob("*.py"))
            elif p.is_file():
                paths.append(p)
        return paths

    # Default: all .py files excluding venv, node_modules, .git
    skip_dirs = {".git", "__pycache__", ".venv", "venv", "node_modules", ".tox"}
    # Exclude this script itself — its string constants contain the bad patterns by design
    skip_files = {ROOT / "check_patterns.py"}
    result = []
    for p in ROOT.rglob("*.py"):
        if any(part in skip_dirs for part in p.parts):
            continue
        if p in skip_files:
            continue
        result.append(p)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bad-pattern linter for the stocks trading system."
    )
    parser.add_argument(
        "files", nargs="*",
        help="Files or directories to scan (default: entire workspace)"
    )
    parser.add_argument(
        "--changed", action="store_true",
        help="Scan only git-staged/modified .py files"
    )
    parser.add_argument(
        "--fix-hints", action="store_true", default=True,
        help="Show fix hints (default: on)"
    )
    parser.add_argument(
        "--no-fix-hints", action="store_false", dest="fix_hints"
    )
    parser.add_argument(
        "--summary-only", action="store_true",
        help="Show only the summary count, no per-hit details"
    )
    args = parser.parse_args()

    files = collect_files(args.files, args.changed)
    if not files:
        print("check_patterns: no .py files to scan.")
        return 0

    all_hits: list[dict] = []
    for f in sorted(files):
        all_hits.extend(scan_file(f))

    if not all_hits:
        print(f"check_patterns: {len(files)} files scanned — no bad patterns found. ✓")
        return 0

    if not args.summary_only:
        # Group by file for readable output
        by_file: dict[str, list[dict]] = {}
        for h in all_hits:
            by_file.setdefault(h["file"], []).append(h)

        for fname, hits in sorted(by_file.items()):
            for h in hits:
                print(f"{fname}:{h['line']}: [{h['id']}] {h['message']}")
                print(f"  Code: {h['code']}")
                if args.fix_hints:
                    print(f"  Fix:  {h['fix']}")
                print()

    total = len(all_hits)
    files_hit = len({h["file"] for h in all_hits})
    print(
        f"check_patterns: {len(files)} files scanned — "
        f"{total} bad pattern(s) found in {files_hit} file(s). ✗"
    )
    print("See .github/known-bugs-and-pitfalls.md for full context on each pattern.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
