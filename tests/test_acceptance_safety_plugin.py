"""Lifecycle checks for the offline acceptance safety plugin."""

from __future__ import annotations

import importlib
from types import ModuleType

import pytest


plugin = importlib.import_module("tests.acceptance_safety_plugin")


class IdentityOwner:
    pass


@pytest.fixture
def isolated_plugin_state():
    saved = {
        "originals": plugin._ORIGINALS,
        "installed": plugin._INSTALLED,
        "stubs_neutralized": plugin._STUBS_NEUTRALIZED,
        "path_setup_neutralized": plugin._PATH_SETUP_NEUTRALIZED,
    }
    plugin._ORIGINALS = {}
    plugin._INSTALLED = False
    plugin._STUBS_NEUTRALIZED = {}
    plugin._PATH_SETUP_NEUTRALIZED = False
    try:
        yield
    finally:
        try:
            plugin._restore()
        finally:
            plugin._ORIGINALS = saved["originals"]
            plugin._INSTALLED = saved["installed"]
            plugin._STUBS_NEUTRALIZED = saved["stubs_neutralized"]
            plugin._PATH_SETUP_NEUTRALIZED = saved["path_setup_neutralized"]


def test_install_is_idempotent_and_restores_exact_originals(isolated_plugin_state):
    class Owner:
        value = object()

    owner = Owner()
    original = owner.value
    bindings = [(owner, "value", lambda old, name: (lambda: old), "fake.value")]
    plugin._install(bindings)
    installed = owner.value
    saved = dict(plugin._ORIGINALS)

    plugin._install(bindings)
    assert owner.value is installed
    assert plugin._ORIGINALS == saved

    try:
        plugin._restore()
        assert owner.value is original
        assert plugin._ORIGINALS == {}
        assert plugin._INSTALLED is False
    finally:
        assert plugin._ORIGINALS == {}


def test_partial_install_failure_restores_everything(isolated_plugin_state):
    class FailingOwner:
        def __init__(self):
            object.__setattr__(self, "blocked", object())
            object.__setattr__(self, "reject_next", True)

        def __setattr__(self, name, value):
            if name == "blocked" and self.reject_next:
                object.__setattr__(self, "reject_next", False)
                raise RuntimeError("synthetic install failure")
            object.__setattr__(self, name, value)

    good = IdentityOwner()
    good.value = object()
    failing = FailingOwner()
    good_original = good.value
    failing_original = failing.blocked
    bindings = [
        (good, "value", lambda old, name: (lambda: old), "good.value"),
        (failing, "blocked", lambda old, name: (lambda: old), "failing.blocked"),
    ]
    with pytest.raises(RuntimeError, match="synthetic install failure"):
        plugin._install(bindings)
    assert good.value is good_original
    assert failing.blocked is failing_original
    assert plugin._ORIGINALS == {}
    assert plugin._INSTALLED is False


def test_conftest_aliases_are_neutralized_once_and_restored_exactly(
    isolated_plugin_state,
):
    class PluginManager:
        def __init__(self, plugins):
            self.plugins = plugins

        def get_plugins(self):
            return self.plugins

    class Config:
        def __init__(self, plugins):
            self.pluginmanager = PluginManager(plugins)

    modules = []
    originals = {}
    for name in ("conftest", "conftest_alias"):
        module = ModuleType(name)
        module.__file__ = str(plugin.ROOT / "conftest.py")
        module.pytest = pytest
        module._write_stub = lambda path, content: (path, content)
        exec(
            "@pytest.fixture(scope='session', autouse=True)\n"
            "def _ensure_reports_exist():\n"
            "    return _write_stub(None, None)\n",
            module.__dict__,
        )
        modules.append(module)
        originals[module] = module._write_stub

    config = Config(modules + [ModuleType("unrelated")])
    plugin._disable_repository_report_stubs(config)
    saved = dict(plugin._ORIGINALS)

    assert set(plugin._STUBS_NEUTRALIZED) == set(modules)
    for module in modules:
        assert module._write_stub is not originals[module]
        underlying = plugin._unwrap_fixture_function(module._ensure_reports_exist)
        assert underlying.__globals__["_write_stub"] is module._write_stub
    assert not hasattr(config.pluginmanager.plugins[-1], "_write_stub")

    plugin._disable_repository_report_stubs(config)
    assert plugin._ORIGINALS == saved

    try:
        plugin._restore()
        for module in modules:
            assert module._write_stub is originals[module]
        assert plugin._STUBS_NEUTRALIZED == {}
    finally:
        assert plugin._ORIGINALS == {}


def test_repeated_path_setup_neutralization_saves_once(isolated_plugin_state):
    owner = IdentityOwner()
    owner.ensure_paths = lambda: "original"
    original = owner.ensure_paths
    plugin._disable_path_setup(owner)
    neutralized = owner.ensure_paths
    saved = dict(plugin._ORIGINALS)

    plugin._disable_path_setup(owner)
    assert owner.ensure_paths is neutralized
    assert plugin._ORIGINALS == saved

    try:
        plugin._restore()
        assert owner.ensure_paths is original
    finally:
        assert plugin._ORIGINALS == {}


def test_configuration_failure_rolls_back_partial_install(
    monkeypatch,
    isolated_plugin_state,
):
    class Config:
        def add_cleanup(self, cleanup):
            self.cleanup = cleanup

    good = IdentityOwner()
    good.value = object()
    original = good.value
    bindings = [
        (good, "value", lambda old, name: (lambda: old), "good.value"),
    ]
    monkeypatch.setattr(plugin, "_binding_specs", lambda: bindings)
    originals = {
        (owner, name): getattr(owner, name)
        for owner, name, _, _ in plugin._binding_specs()
    }
    monkeypatch.setattr(
        plugin,
        "_disable_path_setup",
        lambda: (_ for _ in ()).throw(RuntimeError("configuration failure")),
    )
    try:
        with pytest.raises(RuntimeError, match="configuration failure"):
            plugin.pytest_configure(Config())
        assert plugin._ORIGINALS == {}
        assert plugin._INSTALLED is False
        for (owner, name), saved in originals.items():
            assert getattr(owner, name) is saved
        assert good.value is original
    finally:
        assert plugin._ORIGINALS == {}


def test_restore_attempts_all_bindings_and_reports_failures(isolated_plugin_state):
    class RestoreFailureOwner:
        def __init__(self, label, attempts, fails=False):
            object.__setattr__(self, "label", label)
            object.__setattr__(self, "attempts", attempts)
            object.__setattr__(self, "fails", fails)
            object.__setattr__(self, "value", "replacement")

        def __setattr__(self, name, value):
            if name == "value":
                self.attempts.append(self.label)
                if self.fails:
                    raise RuntimeError("synthetic restoration failure")
            object.__setattr__(self, name, value)

    attempts = []
    failing = RestoreFailureOwner("failing", attempts, fails=True)
    restored = RestoreFailureOwner("restored", attempts)
    restored_original = "original"
    plugin._ORIGINALS[(restored, "value")] = restored_original
    plugin._ORIGINALS[(failing, "value")] = "original"

    with pytest.raises(RuntimeError, match="synthetic restoration failure"):
        plugin._restore()

    assert attempts == ["failing", "restored"]
    assert restored.value is restored_original
    assert plugin._ORIGINALS == {}
    assert plugin._INSTALLED is False
