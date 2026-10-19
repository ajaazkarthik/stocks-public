"""Execute production AST seams without importing the live/network pipeline."""
import ast
import sqlite3
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def source_tree(path):
    return ast.parse((ROOT / path).read_text())


def execute(nodes, env):
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "production-seam", "exec"), env)


def test_collection_is_gated_on_durable_retention_and_copies_fire():
    """A fire may only be collected as completed once its recoverable payload
    was durably retained. Otherwise a later batch persist_run + mark_scored
    would suppress the fire with no scoring payload to recover from."""
    tree = source_tree("tradingview/signal_stack.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == "_completed_scoring_fires.append":
            seam = node
            break
    else:
        pytest.fail("missing successful scoring seam")

    # The append must sit inside an `if _durable_result:` guard.
    guard = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.If) and ast.unparse(n.test) == "_durable_result" and seam in n.body
    )
    assert guard is not None

    # The guard flag must be set only after persist_completed succeeds.
    source = (ROOT / "tradingview/signal_stack.py").read_text()
    persist_index = source.index("_persist_done(_inc_db, fire, _analysis_run_id, result_item)")
    flag_index = source.index("_durable_result = True", persist_index)
    assert flag_index > persist_index, "durable flag set before persistence succeeded"

    # The unretained branch must exist so the fire is retried, not suppressed.
    assert "_unretained_scoring_fires.append" in source

    # Collection copies the fire rather than aliasing it.
    fire = {"pro_symbol": "NASDAQ:A", "fire_time": "t1", "resolution": "60"}
    env = {"fire": fire, "_completed_scoring_fires": []}
    execute([seam], env)
    fire["fire_time"] = "changed"
    assert env["_completed_scoring_fires"][0]["fire_time"] == "t1"


@pytest.mark.parametrize("new_only, insights, has_batch, completed, expected", [
    (True, False, True, True, 1),
    (True, True, True, True, 0),
    (False, False, True, True, 0),
    (True, False, False, True, 0),
    (True, False, True, False, 0),
])
def test_actual_final_marking_gate(monkeypatch, new_only, insights, has_batch, completed, expected):
    tree = source_tree("tradingview/signal_stack.py")
    gate = next(n for n in ast.walk(tree) if isinstance(n, ast.If) and "_completed_scoring_fires" in ast.unparse(n.test))
    calls = []
    fake = ModuleType("database.fires_ledger")
    fake.mark_scored = lambda fires: calls.append(fires) or len(fires)
    monkeypatch.setitem(sys.modules, "database.fires_ledger", fake)
    completed_fire = {"pro_symbol": "NASDAQ:A", "fire_time": "t1", "resolution": "60"}
    # The batch also contains failed/filtered/deduplicated/unselected candidates.
    batch = [completed_fire] + [{"pro_symbol": name} for name in ("failed", "filtered", "deduplicated", "unselected")]
    env = {"args": SimpleNamespace(new_only=new_only, insights=insights),
           "_new_only_batch": batch if has_batch else [],
           "_completed_scoring_fires": [completed_fire] if completed else []}
    execute([gate], env)
    assert len(calls) == expected
    if expected:
        assert calls == [[completed_fire]]


def test_real_ledger_sql_updates_only_completed_identity():
    tree = source_tree("database/fires_ledger.py")
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE downloaded_fires (pro_symbol TEXT, fire_time TEXT, resolution TEXT, scored_at TEXT)")
        rows = [("NASDAQ:A", "t1", "60"), ("NASDAQ:A", "t2", "60"), ("NASDAQ:A", "t1", "D"), ("NASDAQ:B", "t1", "60")]
        conn.executemany("INSERT INTO downloaded_fires VALUES (?, ?, ?, NULL)", rows)
        env = {"_conn": lambda: conn, "_now_iso": lambda: "completion-time"}
        execute([n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("fire_key", "mark_scored")], env)
        done = dict(zip(("pro_symbol", "fire_time", "resolution"), rows[0]))
        assert env["mark_scored"]([done]) == 1
        assert env["mark_scored"]([done]) == 0
        assert env["mark_scored"]([]) == 0
        actual = conn.execute("SELECT pro_symbol, fire_time, resolution, scored_at FROM downloaded_fires").fetchall()
        assert actual == [(*rows[0], "completion-time")] + [(*row, None) for row in rows[1:]]
    finally:
        conn.close()
