"""Hermes runtime isolation tests.

These tests prove the runtime isolation contract:

- Missing or incompatible Hermes APIs fail closed with safe defaults and
  redacted logs (no exception text, paths, or payloads).
- The plugin API module loads without a Hermes installation.
"""

from __future__ import annotations

import builtins
import importlib
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = REPO_ROOT / "dashboard"


def load_plugin_api():
    """Load dashboard/plugin_api.py under the exact module name Hermes uses."""
    existing = sys.modules.get("plugin_api")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        "plugin_api",
        DASHBOARD_DIR / "plugin_api.py",
        submodule_search_locations=[str(DASHBOARD_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not create a module spec for plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["plugin_api"] = module
    spec.loader.exec_module(module)
    return module


def test_plugin_api_loads_without_hermes_installed(monkeypatch):
    """The plugin API and its providers import with Hermes imports blocked."""
    blocked_prefixes = ("hermes_cli", "hermes_constants", "agent")
    real_import = builtins.__import__

    def blocking_import(name: str, *args: Any, **kwargs: Any):
        if name.startswith("plugin_api.") or name == "plugin_api":
            return real_import(name, *args, **kwargs)
        if any(name == prefix or name.startswith(prefix + ".") for prefix in blocked_prefixes):
            raise ImportError(f"blocked Hermes import: {name}")
        return real_import(name, *args, **kwargs)

    for key in list(sys.modules):
        if key == "plugin_api" or key.startswith("plugin_api."):
            del sys.modules[key]
    monkeypatch.setattr(builtins, "__import__", blocking_import)
    try:
        api = load_plugin_api()
    finally:
        monkeypatch.undo()

    # The module imported, which is itself the proof that no module-level
    # Hermes import exists. Its runtime adapter must still be wired up.
    assert api.runtime is not None
    assert api.PROVIDERS


class _ImportBlocker:
    """Context manager that makes one or more packages unimportable."""

    def __init__(self, monkeypatch, prefixes: tuple[str, ...]):
        self._monkeypatch = monkeypatch
        self._prefixes = prefixes
        self._real_import = builtins.__import__

    def __enter__(self) -> "_ImportBlocker":
        def blocking(name: str, *args: Any, **kwargs: Any):
            if any(name == p or name.startswith(p + ".") for p in self._prefixes):
                raise ImportError(f"blocked Hermes import: {name}")
            return self._real_import(name, *args, **kwargs)

        self._monkeypatch.setattr(builtins, "__import__", blocking)
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._monkeypatch.undo()


def test_missing_hermes_apis_fail_closed(monkeypatch, caplog):
    """Every adapter function returns a safe default when Hermes is missing."""
    api = load_plugin_api()
    runtime = api.runtime

    with _ImportBlocker(monkeypatch, ("hermes_cli", "hermes_constants", "agent")):
        with caplog.at_level("WARNING"):
            assert runtime.resolve_provider_credentials("deepseek") is None
            assert runtime.codex_configured() is False
            assert runtime.load_plugin_provider_allowlist() is None
            assert runtime.fetch_account_usage("openai-codex") is None
            assert runtime.list_profiles() == []
            assert runtime.exhausted_until("deepseek", {"last_status": "exhausted"}) is None
            with pytest.raises(runtime.RuntimeUnavailable):
                runtime.normalize_profile_name("worker-1")
            with pytest.raises(runtime.RuntimeUnavailable):
                runtime.clear_codex_usage_cache()

    # Logs must be redacted: feature names only, no exception text or paths.
    for record in caplog.records:
        message = record.getMessage()
        assert "blocked Hermes import" not in message
        assert "Traceback" not in message


def test_incompatible_hermes_api_fails_closed(monkeypatch, caplog):
    """An installed-but-incompatible Hermes API fails closed without details."""
    api = load_plugin_api()
    runtime = api.runtime

    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_profiles = types.ModuleType("hermes_cli.profiles")

    def incompatible_list_profiles():
        raise TypeError("list_profiles() got an unexpected keyword argument")

    fake_profiles.list_profiles = incompatible_list_profiles
    fake_hermes_cli.profiles = fake_profiles
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", fake_profiles)

    with caplog.at_level("WARNING"):
        assert runtime.list_profiles() == []

    for record in caplog.records:
        assert "unexpected keyword argument" not in record.getMessage()
        assert "TypeError" not in record.getMessage()


def test_provider_allowlist_is_read_through_runtime_boundary(monkeypatch):
    """Plugin configuration stays behind the runtime adapter and is normalized."""
    api = load_plugin_api()
    runtime = api.runtime

    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.__path__ = []
    fake_config = types.ModuleType("hermes_cli.config")
    setattr(fake_config, "load_config", lambda: {
        "plugins": {
            "quota-console": {
                "providers": [" MiniMax ", "deepseek", "minimax", None],
            }
        }
    })
    setattr(fake_hermes_cli, "config", fake_config)
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config)

    assert runtime.load_plugin_provider_allowlist() == ("deepseek", "minimax")


def test_codex_usage_cache_clear_uses_adapter_path(monkeypatch, tmp_path):
    """The documented Codex usage cache is cleared through the adapter path."""
    api = load_plugin_api()
    runtime = api.runtime

    cache_file = tmp_path / "codex_usage_state.json"
    cache_file.write_text('{"sessions": [1, 2, 3]}', encoding="utf-8")
    monkeypatch.setattr(runtime, "hermes_default_root", lambda: tmp_path)

    assert runtime.clear_codex_usage_cache() is True
    assert cache_file.read_text(encoding="utf-8") == "{}\n"

    # A missing cache file reports False instead of creating or failing.
    cache_file.unlink()
    assert runtime.clear_codex_usage_cache() is False


def test_codex_usage_cache_fails_closed_without_hermes(monkeypatch):
    api = load_plugin_api()
    runtime = api.runtime

    monkeypatch.setattr(runtime, "hermes_default_root", lambda: None)
    with pytest.raises(runtime.RuntimeUnavailable):
        runtime.clear_codex_usage_cache()


def test_summary_endpoint_returns_safe_schema_without_hermes(monkeypatch):
    """The API answers with a safe, generic summary when Hermes is missing."""
    api = load_plugin_api()
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: [])
    # Block credential resolution too so no live provider call can happen,
    # and isolate the process-wide summary cache from other tests.
    monkeypatch.setattr(api.runtime, "resolve_provider_credentials", lambda provider: None)
    monkeypatch.setattr(api.runtime, "codex_configured", lambda: False)
    monkeypatch.setattr(api, "_CACHE", None)
    monkeypatch.setattr(api, "_CACHE_AT", 0.0)
    summary = api._cached_summary()
    assert set(summary) == {
        "version",
        "updated_at",
        "profiles",
        "providers",
        "provider_overview",
        "alerts",
        "settings",
    }
    assert summary["profiles"] == []
    assert summary["providers"] == []
    assert "token" not in json.dumps(summary)
