"""Regression tests for the brain-usage enforcement hooks.

These cover the deterministic half of the brain protocol: the hook scripts in
`.github/hooks/scripts/` and the hook JSON that wires them up. The failure they
guard against is a hook that silently never fires (Windows-only platform key) or
a guard that fails closed and wedges a session.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / ".github" / "hooks"
SCRIPTS_DIR = HOOKS_DIR / "scripts"
INSTRUCTIONS_DIR = ROOT / ".github" / "instructions"

BRAIN_GUARD = SCRIPTS_DIR / "brain_guard.py"
SESSION_INIT = SCRIPTS_DIR / "session_init.py"
PRE_COMPACT = SCRIPTS_DIR / "pre_compact.py"

HOOK_CONFIGS = sorted(HOOKS_DIR.glob("*.json"))


def _load_brain_guard():
    spec = importlib.util.spec_from_file_location("brain_guard", BRAIN_GUARD)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run(script: Path, payload: dict | str | None, *args: str, env: dict | None = None):
    """Invoke a hook script the way VS Code does: JSON on stdin, JSON on stdout."""
    if payload is None:
        stdin = ""
    elif isinstance(payload, str):
        stdin = payload
    else:
        stdin = json.dumps(payload)

    return subprocess.run(
        [sys.executable, str(script), *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


@pytest.fixture
def vault(tmp_path):
    """A synthetic brain vault with an empty activity log."""
    root = tmp_path / "vault"
    (root / "_Inbox").mkdir(parents=True)
    (root / "_Inbox" / "session-activity-log.md").write_text(
        "# Session Activity Log\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def hook_env(vault, tmp_path, monkeypatch):
    """Point the hook at the synthetic vault and isolate its state file."""
    monkeypatch.setenv("OBSIDIAN_BRAIN_VAULT", str(vault))
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "OBSIDIAN_BRAIN_VAULT": str(vault),
        "HOME": str(tmp_path),
    }
    return env


def _record_save(vault: Path, filename: str = "Engineering/test.md") -> None:
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    with (vault / "_Inbox" / "session-activity-log.md").open("a", encoding="utf-8") as handle:
        handle.write(f"- {stamp} append {filename}\n")


# ---------------------------------------------------------------------------
# Hook wiring: the defect that made every existing hook inert on macOS
# ---------------------------------------------------------------------------
def test_hook_configs_exist():
    assert HOOK_CONFIGS, "no hook configs found in .github/hooks/"


@pytest.mark.parametrize("config_path", HOOK_CONFIGS, ids=lambda p: p.name)
def test_hook_config_is_valid_json(config_path):
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert "hooks" in data and isinstance(data["hooks"], dict)


@pytest.mark.parametrize("config_path", HOOK_CONFIGS, ids=lambda p: p.name)
def test_every_hook_runs_on_macos_and_linux(config_path):
    """A hook with only a `windows` key never fires on macOS or Linux."""
    data = json.loads(config_path.read_text(encoding="utf-8"))
    for event, entries in data["hooks"].items():
        for entry in entries:
            assert entry.get("type") == "command", f"{event}: type must be command"
            portable = bool(entry.get("command"))
            has_osx = bool(entry.get("osx"))
            has_linux = bool(entry.get("linux"))
            assert portable or (has_osx and has_linux), (
                f"{config_path.name}:{event} has no portable `command` and no "
                "osx/linux override, so it will not run on this machine"
            )


@pytest.mark.parametrize("config_path", HOOK_CONFIGS, ids=lambda p: p.name)
def test_hook_scripts_referenced_by_configs_exist(config_path):
    data = json.loads(config_path.read_text(encoding="utf-8"))
    for entries in data["hooks"].values():
        for entry in entries:
            for key in ("command", "windows", "linux", "osx"):
                command = entry.get(key)
                if not command:
                    continue
                for token in command.split():
                    if token.endswith(".py"):
                        assert (ROOT / token).is_file(), f"missing script: {token}"


def test_brain_guard_is_wired_to_post_tool_use_and_stop():
    data = json.loads((HOOKS_DIR / "brain-guard.json").read_text(encoding="utf-8"))
    assert "PostToolUse" in data["hooks"], "no mid-task checkpoint hook"
    assert "Stop" in data["hooks"], "no completion gate hook"


# ---------------------------------------------------------------------------
# brain_guard: parsing and decision logic
# ---------------------------------------------------------------------------
def test_brain_guard_self_test_passes():
    result = _run(BRAIN_GUARD, None, "--self-test")
    assert result.returncode == 0, result.stderr
    assert "all checks passed" in result.stdout


def test_activity_log_parser_reads_individual_saves(vault):
    module = _load_brain_guard()
    (vault / "_Inbox" / "session-activity-log.md").write_text(
        "# Session Activity Log\n\n"
        "- 2026-10-13T02:29:46.482Z append Engineering/old.md\n"
        "- 2026-10-13T03:31:43.580Z append _Inbox/new.md\n"
        "## 2026-10-13T22:54:56.025Z - Checkpoint\n\n"
        "Recent save activity (up to 20):\n"
        "- 2026-10-13T22:46:28.308Z append Engineering/late.md\n",
        encoding="utf-8",
    )
    saves = module.recent_saves(vault)
    assert len(saves) == 3
    assert [s[0] for s in saves] == sorted(s[0] for s in saves)


def test_activity_log_parser_ignores_malformed_lines(vault):
    module = _load_brain_guard()
    (vault / "_Inbox" / "session-activity-log.md").write_text(
        "# Session Activity Log\n"
        "- not a save line\n"
        "- 2026-13-99T99:99:99Z append broken.md\n"
        "- 2026-10-13T03:31:43.580Z append good.md\n",
        encoding="utf-8",
    )
    saves = module.recent_saves(vault)
    assert len(saves) == 1
    assert saves[0][1].endswith("good.md")


def test_missing_activity_log_is_not_an_error(tmp_path):
    module = _load_brain_guard()
    assert module.recent_saves(tmp_path / "does-not-exist") == []


# ---------------------------------------------------------------------------
# brain_guard: event behaviour
# ---------------------------------------------------------------------------
def test_session_start_injects_protocol(hook_env, tmp_path):
    result = _run(
        BRAIN_GUARD,
        {"hook_event_name": "SessionStart", "cwd": str(tmp_path / "ws")},
        env=hook_env,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "BRAIN PROTOCOL" in context
    assert "SEARCH" in context


def test_no_reminder_before_the_threshold(hook_env, tmp_path):
    ws = str(tmp_path / "ws")
    _run(BRAIN_GUARD, {"hook_event_name": "SessionStart", "cwd": ws}, env=hook_env)
    for _ in range(4):
        result = _run(BRAIN_GUARD, {"hook_event_name": "PostToolUse", "cwd": ws}, env=hook_env)
        assert result.stdout.strip() == ""


def test_reminder_fires_at_threshold_without_a_save(hook_env, tmp_path):
    ws = str(tmp_path / "ws")
    _run(BRAIN_GUARD, {"hook_event_name": "SessionStart", "cwd": ws}, env=hook_env)
    outputs = [
        _run(BRAIN_GUARD, {"hook_event_name": "PostToolUse", "cwd": ws}, env=hook_env).stdout
        for _ in range(5)
    ]
    assert outputs[:4] == ["", "", "", ""]
    payload = json.loads(outputs[4])
    assert payload["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "BRAIN CHECKPOINT DUE" in payload["hookSpecificOutput"]["additionalContext"]


def test_no_reminder_when_a_save_exists(hook_env, tmp_path, vault):
    ws = str(tmp_path / "ws")
    _run(BRAIN_GUARD, {"hook_event_name": "SessionStart", "cwd": ws}, env=hook_env)
    _record_save(vault)
    for _ in range(10):
        result = _run(BRAIN_GUARD, {"hook_event_name": "PostToolUse", "cwd": ws}, env=hook_env)
        assert result.stdout.strip() == ""


def test_stop_blocks_once_when_nothing_was_saved(hook_env, tmp_path):
    ws = str(tmp_path / "ws")
    _run(BRAIN_GUARD, {"hook_event_name": "SessionStart", "cwd": ws}, env=hook_env)

    first = _run(BRAIN_GUARD, {"hook_event_name": "Stop", "cwd": ws}, env=hook_env)
    payload = json.loads(first.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert payload["hookSpecificOutput"]["decision"] == "block"
    assert "brain save" in payload["hookSpecificOutput"]["reason"].lower()

    second = _run(BRAIN_GUARD, {"hook_event_name": "Stop", "cwd": ws}, env=hook_env)
    assert second.stdout.strip() == "", "the completion gate must not block twice"


def test_stop_honours_stop_hook_active(hook_env, tmp_path):
    """A re-entrant Stop must not block again, or the agent loops forever."""
    ws = str(tmp_path / "ws")
    _run(BRAIN_GUARD, {"hook_event_name": "SessionStart", "cwd": ws}, env=hook_env)
    result = _run(
        BRAIN_GUARD,
        {"hook_event_name": "Stop", "cwd": ws, "stop_hook_active": True},
        env=hook_env,
    )
    assert result.stdout.strip() == ""


def test_stop_does_not_block_when_a_save_exists(hook_env, tmp_path, vault):
    ws = str(tmp_path / "ws")
    _run(BRAIN_GUARD, {"hook_event_name": "SessionStart", "cwd": ws}, env=hook_env)
    _record_save(vault)
    result = _run(BRAIN_GUARD, {"hook_event_name": "Stop", "cwd": ws}, env=hook_env)
    assert result.stdout.strip() == ""


def test_pre_compact_reports_save_count(hook_env, tmp_path, vault):
    ws = str(tmp_path / "ws")
    _run(BRAIN_GUARD, {"hook_event_name": "SessionStart", "cwd": ws}, env=hook_env)
    _record_save(vault)
    result = _run(BRAIN_GUARD, {"hook_event_name": "PreCompact", "cwd": ws}, env=hook_env)
    payload = json.loads(result.stdout)
    assert "Brain saves this session: 1" in payload["systemMessage"]


def test_event_name_is_read_from_stdin_without_the_cli_flag(hook_env, tmp_path):
    result = _run(
        BRAIN_GUARD,
        {"hook_event_name": "SessionStart", "cwd": str(tmp_path / "ws")},
        env=hook_env,
    )
    assert "BRAIN PROTOCOL" in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


def test_cli_event_flag_overrides_stdin(hook_env, tmp_path):
    result = _run(
        BRAIN_GUARD,
        {"hook_event_name": "Stop", "cwd": str(tmp_path / "ws")},
        "--event",
        "SessionStart",
        env=hook_env,
    )
    assert "BRAIN PROTOCOL" in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


# ---------------------------------------------------------------------------
# Fail-open guarantees: a broken hook must never wedge a session
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_input", ["", "   ", "not json", "[]", "null", '{"a": 1}'])
def test_malformed_stdin_exits_cleanly(hook_env, bad_input):
    result = _run(BRAIN_GUARD, bad_input, env=hook_env)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_unknown_event_exits_cleanly(hook_env):
    result = _run(BRAIN_GUARD, {"hook_event_name": "NotARealEvent"}, env=hook_env)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_missing_vault_does_not_break_the_hook(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "OBSIDIAN_BRAIN_VAULT": str(tmp_path / "nope")}
    ws = str(tmp_path / "ws")
    _run(BRAIN_GUARD, {"hook_event_name": "SessionStart", "cwd": ws}, env=env)
    for _ in range(6):
        result = _run(BRAIN_GUARD, {"hook_event_name": "PostToolUse", "cwd": ws}, env=env)
        assert result.returncode == 0
    stop = _run(BRAIN_GUARD, {"hook_event_name": "Stop", "cwd": ws}, env=env)
    assert stop.returncode == 0


# ---------------------------------------------------------------------------
# Companion hook scripts
# ---------------------------------------------------------------------------
def test_session_init_runs_and_exits_zero():
    result = _run(SESSION_INIT, None)
    assert result.returncode == 0, result.stderr


def test_pre_compact_runs_and_exits_zero():
    result = _run(PRE_COMPACT, None)
    assert result.returncode == 0, result.stderr
    assert "CONTEXT COMPACTION" in result.stdout


def test_session_init_progress_excerpt_keeps_only_live_sections():
    spec = importlib.util.spec_from_file_location("session_init", SESSION_INIT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    text = (
        "# PROGRESS\n\n"
        "## Done\n- historical item\n\n"
        "## In Progress\n- current item\n\n"
        "## Next\n- next item\n"
    )
    excerpt = module.progress_excerpt(text)
    assert "## In Progress" in excerpt
    assert "## Next" in excerpt
    assert "historical item" not in excerpt


# ---------------------------------------------------------------------------
# Instruction placement: the dead-file defect
# ---------------------------------------------------------------------------
def test_brain_workflow_instruction_is_in_a_live_location():
    live = INSTRUCTIONS_DIR / "brain-first-workflow.instructions.md"
    assert live.is_file(), (
        "the brain workflow instruction must live in .github/instructions/ "
        "to be loaded; prompt/ is not a customization folder"
    )


@pytest.mark.parametrize(
    "instruction", sorted(INSTRUCTIONS_DIR.glob("*.instructions.md")), ids=lambda p: p.name
)
def test_instruction_frontmatter_is_valid(instruction):
    text = instruction.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{instruction.name} is missing frontmatter"
    _, frontmatter, _ = text.split("---", 2)
    assert "description:" in frontmatter, f"{instruction.name} has no description"
    assert "applyTo:" in frontmatter, f"{instruction.name} has no applyTo"


def test_brain_workflow_instruction_states_the_cadence():
    text = (INSTRUCTIONS_DIR / "brain-first-workflow.instructions.md").read_text(encoding="utf-8")
    assert "3–5 tool calls" in text or "3-5 tool calls" in text
    assert "task_complete" in text
