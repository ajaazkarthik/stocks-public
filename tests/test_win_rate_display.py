"""Tests for H-09: verify stale win rate display strings removed from alert_tracker.py."""


def test_no_stale_win_rate_displays():
    """_print_stats must NOT show 'Win rate (closed)' or 'Effective rate' labels (H-09)."""
    with open("tradingview/alert_tracker.py", encoding="utf-8") as f:
        source = f.read()

    # These specific print()-display strings should be removed
    assert 'Win rate (closed)' not in source, (
        "Old 'Win rate (closed)' display found in source"
    )
    # Only the HTML template version is acceptable (in an HTML string, not a print statement)
    # Check that no print() statement uses "Effective rate"
    import ast
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "print":
                for arg in node.args:
                    if isinstance(arg, ast.JoinedStr):
                        # f-string — check its string parts
                        for val in ast.walk(arg):
                            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                                assert "Effective rate" not in val.value, (
                                    f"Found 'Effective rate' in a print() statement at line {node.lineno}"
                                )
                    elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        assert "Effective rate" not in arg.value, (
                            f"Found 'Effective rate' in a print() statement at line {node.lineno}"
                        )
