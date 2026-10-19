"""Fix the import path in _compute_option_score helper in options_hedger.py."""
import pathlib

SRC = pathlib.Path("options_hedger.py")
text = SRC.read_text(encoding="utf-8")

OLD = (
    '        _oa_dir = str(_pathlib.Path(__file__).parent.parent / "options")\n'
    '        if _oa_dir not in _oa_sys.path:\n'
    '            _oa_sys.path.insert(0, _oa_dir)\n'
    '        from options_analyzer import (  # type: ignore\n'
    '            _composite_option_score, _iv_score, _strike_score,\n'
    '            _dte_score, _liquidity_score, MODE_CONFIGS, StrategyMode,\n'
    '        )'
)

NEW = (
    '        _oa_dir = str(_pathlib.Path(__file__).parent.parent)  # tradingview/\n'
    '        if _oa_dir not in _oa_sys.path:\n'
    '            _oa_sys.path.insert(0, _oa_dir)\n'
    '        from options.options_analyzer import (  # type: ignore\n'
    '            _composite_option_score, _iv_score, _strike_score,\n'
    '            _dte_score, _liquidity_score, MODE_CONFIGS, StrategyMode,\n'
    '        )'
)

assert OLD in text, f"OLD not found. Check: {repr(text[text.find('_oa_dir')-5:text.find('_oa_dir')+120])}"
text = text.replace(OLD, NEW, 1)
SRC.write_text(text, encoding="utf-8")
print("Fixed import path: tradingview/ root, from options.options_analyzer import ...")
