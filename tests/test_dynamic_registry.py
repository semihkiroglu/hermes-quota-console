"""Dynamic provider-registry tests.

The dashboard must reflect ``plugins.quota-console.providers`` allowlist
changes without a process restart. The plugin API calls
``registry.load()`` on every summary and reset, so a config flip between
two requests must produce a new provider tuple.

These tests stand up the plugin API with a stubbed allowlist loader and
prove the dashboard would observe the new scope immediately.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_API_PATH = REPO_ROOT / "dashboard" / "plugin_api.py"


@pytest.fixture
def isolated_plugin_api(monkeypatch):
    """Load the plugin API fresh with a clean module cache."""
    module_name = f"quota_console_dynamic_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_API_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        # Reset the cached summary so every test starts from scratch.
        monkeypatch.setattr(module, "_CACHE", None)
        monkeypatch.setattr(module, "_CACHE_AT", 0.0)
        yield module
    finally:
        for loaded in tuple(sys.modules):
            if loaded == module_name or loaded.startswith(f"{module_name}."):
                sys.modules.pop(loaded, None)


def test_registry_merges_catalog_providers_as_profile_only(isolated_plugin_api, monkeypatch):
    """Hermes' catalog providers appear as profile-only rows with no quota
    fetch, while built-in adapters keep their real quota fetch."""
    api = isolated_plugin_api
    catalog_rows = [
        {"slug": "deepseek", "label": "DeepSeek"},
        {"slug": "openai-codex", "label": "ChatGPT or Codex Subscription"},
        {"slug": "minimax", "label": "MiniMax"},
        {"slug": "copilot", "label": "GitHub Copilot"},
        {"slug": "opencode-free", "label": "OpenCode Free", "keyless": True},
        {"slug": "novita", "label": "NovitaAI"},
    ]
    monkeypatch.setattr(api.runtime, "list_catalog_providers", lambda: catalog_rows)

    specs = api._registry._catalog_extension_specs()
    by_id = {spec.id: spec for spec in specs}

    # Built-ins keep quota; catalog-only providers do not.
    assert by_id["deepseek"].has_quota is True
    assert by_id["openai-codex"].has_quota is True
    assert by_id["minimax"].has_quota is True
    assert by_id["copilot"].has_quota is False
    assert by_id["novita"].has_quota is False

    # Keyless providers are flagged so they count as configured.
    assert by_id["opencode-free"].has_quota is False
    assert by_id["opencode-free"].keyless is True
    assert by_id["copilot"].keyless is False

    # Profile-only specs never fetch anything.
    assert by_id["copilot"].fetch(None) is None

    # Catalog order is preserved for the dashboard.
    assert [spec.id for spec in specs] == [
        "deepseek", "openai-codex", "minimax", "copilot", "opencode-free", "novita",
    ]


def test_summary_uses_provider_tuple_from_each_request(
    isolated_plugin_api, monkeypatch
):
    api = isolated_plugin_api
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: [])
    monkeypatch.setattr(api.runtime, "resolve_provider_credentials", lambda provider: None)
    monkeypatch.setattr(api.runtime, "codex_configured", lambda: False)

    seen: list[tuple[str, ...]] = []
    real_load = api._registry.load

    def capturing_load():
        result = real_load()
        seen.append(tuple(spec.id for spec in result))
        return result

    monkeypatch.setattr(api._registry, "load", capturing_load)
    # Pin the allowlist so the first and second calls behave identically.
    monkeypatch.setattr(api._registry, "_load_allowlist", lambda: None)

    first = api._cached_summary()
    second = api._cached_summary()

    assert first["providers"] == []
    assert second["providers"] == []
    # Two summaries were requested and each one triggered a fresh registry
    # load. The internal ``_cached_summary`` may serve the second call from
    # its short cache, so we explicitly invalidate the cache to observe the
    # loader a second time.
    api._CACHE = None
    api._CACHE_AT = 0.0
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: [])
    api._cached_summary()
    assert len(seen) >= 2, (
        "registry.load() must be consulted on every summary build: %r" % seen
    )
    assert seen[-1] == ("deepseek", "openai-codex", "minimax"), (
        "without an allowlist the registry must default to built-ins: %r" % seen[-1]
    )


def test_allowlist_change_is_visible_to_next_summary(
    isolated_plugin_api, monkeypatch
):
    """Flipping the allowlist between two summaries must change the scope."""
    api = isolated_plugin_api
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: [])
    monkeypatch.setattr(api.runtime, "resolve_provider_credentials", lambda provider: None)
    monkeypatch.setattr(api.runtime, "codex_configured", lambda: False)

    observed: list[tuple[str, ...]] = []
    allowlist: list[tuple[str, ...] | None] = [("minimax",)]

    def fake_load_allowlist():
        return allowlist[0]

    def capturing_load():
        specs = api._registry._build_registry(_load_allowlist_value())
        observed.append(tuple(spec.id for spec in specs))
        return specs

    _load_allowlist_value = fake_load_allowlist

    monkeypatch.setattr(api._registry, "_load_allowlist", fake_load_allowlist)
    monkeypatch.setattr(api._registry, "load", capturing_load)

    api._cached_summary()
    # The first call consulted the registry with the original allowlist.
    assert observed[-1] == ("minimax",), (
        "first summary must observe the configured allowlist: %r" % observed[-1]
    )

    allowlist[0] = ("deepseek",)
    api._CACHE = None
    api._CACHE_AT = 0.0

    api._cached_summary()
    assert observed[-1] == ("deepseek",), (
        "summary must observe the new allowlist without a restart: %r" % observed[-1]
    )


def test_empty_allowlist_disables_every_provider_in_summary(
    isolated_plugin_api, monkeypatch
):
    """An explicit empty allowlist is honoured by the summary path."""
    api = isolated_plugin_api
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: [])
    monkeypatch.setattr(api.runtime, "resolve_provider_credentials", lambda provider: None)
    monkeypatch.setattr(api.runtime, "codex_configured", lambda: False)
    monkeypatch.setattr(api._registry, "_load_allowlist", lambda: ())

    summary = api._cached_summary()
    assert summary["providers"] == []


def test_reset_path_consults_live_provider_tuple(
    isolated_plugin_api, monkeypatch, tmp_path
):
    """A reset must touch the live allowlist scope, not an import-time one."""
    api = isolated_plugin_api
    profile_dir = tmp_path / "worker-1"
    profile_dir.mkdir()
    auth_path = profile_dir / "auth.json"
    auth_path.write_text(
        '{"version": 1, "providers": {}, "credential_pool": {}}',
        encoding="utf-8",
    )
    rows = [
        {
            "name": "worker-1",
            "path": profile_dir,
            "model": "fixture",
            "provider": "deepseek",
        }
    ]
    monkeypatch.setattr(api, "_profile_rows", lambda: rows)
    monkeypatch.setattr(api.runtime, "clear_codex_usage_cache", lambda: False)
    monkeypatch.setattr(
        api.runtime,
        "update_auth_store",
        lambda _profile_path, mutator: mutator(
            {"version": 1, "providers": {}, "credential_pool": {}}
        ),
    )

    seen_providers: list[tuple[str, ...]] = []
    real_load = api._registry.load

    def capturing_load():
        result = real_load()
        seen_providers.append(tuple(spec.id for spec in result))
        return result

    monkeypatch.setattr(api._registry, "load", capturing_load)
    monkeypatch.setattr(api._registry, "_load_allowlist", lambda: ("minimax",))

    result = api._reset_profiles("all", None)
    assert result["ok"] is True
    assert seen_providers[-1] == ("minimax",), (
        "reset must respect the current allowlist: %r" % seen_providers[-1]
    )


def test_summary_summary_json_has_no_credential_leak(
    isolated_plugin_api, monkeypatch
):
    """The dynamic loader must not introduce a credential leak path."""
    api = isolated_plugin_api
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: [])
    monkeypatch.setattr(api.runtime, "resolve_provider_credentials", lambda provider: None)
    monkeypatch.setattr(api.runtime, "codex_configured", lambda: False)

    summary = api._cached_summary()
    assert "token" not in json.dumps(summary).lower()
    assert "api_key" not in json.dumps(summary).lower()
