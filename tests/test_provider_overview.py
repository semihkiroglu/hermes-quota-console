"""Provider-first overview tests.

The provider-first view is the primary dashboard layout. The API
exposes a ``provider_overview`` field that groups live profile cards
under their configured provider while preserving each provider's quota
snapshot.

These tests fail closed the moment the field disappears, the structure
regresses, or a credential value leaks into the grouping payload.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_API_PATH = REPO_ROOT / "dashboard" / "plugin_api.py"


@pytest.fixture
def isolated_plugin_api(monkeypatch):
    module_name = f"quota_console_overview_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_API_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "_CACHE", None)
        monkeypatch.setattr(module, "_CACHE_AT", 0.0)
        yield module
    finally:
        for loaded in tuple(sys.modules):
            if loaded == module_name or loaded.startswith(f"{module_name}."):
                sys.modules.pop(loaded, None)


def test_summary_includes_provider_overview(isolated_plugin_api, monkeypatch):
    api = isolated_plugin_api
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: [])
    monkeypatch.setattr(api.runtime, "resolve_provider_credentials", lambda provider: None)
    monkeypatch.setattr(api.runtime, "codex_configured", lambda: False)

    summary = api._cached_summary()
    assert "provider_overview" in summary, (
        "summary must include the provider_overview field"
    )
    assert [bucket["id"] for bucket in summary["provider_overview"]] == [
        "deepseek",
        "openai-codex",
        "minimax",
    ]
    assert all(bucket["profiles"] == [] for bucket in summary["provider_overview"])
    assert all("settings" in bucket for bucket in summary["provider_overview"])
    # Every bucket exposes its alert shape; the top-level summary
    # carries the aggregated low/exhausted inputs for the yellow/red alerts.
    assert all("alert" in bucket for bucket in summary["provider_overview"])
    assert set(summary) == {
        "version",
        "updated_at",
        "profiles",
        "providers",
        "provider_overview",
        "alerts",
        "settings",
    }


def test_provider_overview_groups_profile_cards(isolated_plugin_api, monkeypatch):
    api = isolated_plugin_api
    rows = [
        {"name": "default", "path": Path("/tmp/default"), "model": "gpt", "provider": "deepseek"},
        {"name": "worker-1", "path": Path("/tmp/worker-1"), "model": "deepseek-v4-flash", "provider": "deepseek"},
        {"name": "worker-2", "path": Path("/tmp/worker-2"), "model": "deepseek-v4-pro", "provider": "deepseek"},
        {"name": "ops-bot", "path": Path("/tmp/ops-bot"), "model": "MiniMax-M3", "provider": "minimax"},
    ]
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: rows)
    monkeypatch.setattr(api.runtime, "resolve_provider_credentials", lambda provider: None)
    monkeypatch.setattr(api.runtime, "codex_configured", lambda: False)
    monkeypatch.setattr(api.runtime, "read_auth_store", lambda _path: {"providers": {}, "credential_pool": {}})

    overview = api._build_provider_overview(
        api._build_model_cards(),
        api._current_providers(),
        {},
    )

    bucket_ids = [bucket["id"] for bucket in overview]
    assert bucket_ids == ["deepseek", "openai-codex", "minimax"]

    deepseek_bucket = next(b for b in overview if b["id"] == "deepseek")
    profile_names = [p["profile"] for p in deepseek_bucket["profiles"]]
    assert profile_names == ["default", "worker-1", "worker-2"], (
        "default must stay first, then profiles are sorted by name: %r" % profile_names
    )
    for item in deepseek_bucket["profiles"]:
        assert item["status"] == "unconfigured"
        assert item["model"]


def test_provider_overview_skips_profiles_without_provider(
    isolated_plugin_api, monkeypatch
):
    """A profile with no configured provider must not appear in the overview."""
    api = isolated_plugin_api
    rows = [
        {"name": "configured", "path": Path("/tmp/a"), "model": "m", "provider": "deepseek"},
        {"name": "missing-provider", "path": Path("/tmp/b"), "model": "m", "provider": None},
    ]
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: rows)
    monkeypatch.setattr(api.runtime, "resolve_provider_credentials", lambda provider: None)
    monkeypatch.setattr(api.runtime, "codex_configured", lambda: False)
    monkeypatch.setattr(api.runtime, "read_auth_store", lambda _path: {"providers": {}, "credential_pool": {}})

    overview = api._build_provider_overview(
        api._build_model_cards(),
        api._current_providers(),
        {},
    )
    deepseek = next(bucket for bucket in overview if bucket["id"] == "deepseek")
    profile_names = [p["profile"] for p in deepseek["profiles"]]
    assert profile_names == ["configured"]
    assert "missing-provider" not in json.dumps(overview)


def test_provider_overview_preserves_provider_quota_snapshot(
    isolated_plugin_api, monkeypatch
):
    """Removing the standalone quota section must not drop quota data."""
    api = isolated_plugin_api
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: [])
    provider_cards = {
        "deepseek": {
            "id": "deepseek",
            "label": "DeepSeek",
            "status": "ok",
            "plan": None,
            "windows": [{"label": "PAYG", "remaining_percent": 50}],
            "balances": [{"label": "Balance", "currency": "USD", "amount": 1.25}],
            "notice": "safe note",
        }
    }

    overview = api._build_provider_overview(
        api._build_model_cards(),
        api._current_providers(),
        provider_cards,
    )

    deepseek = next(bucket for bucket in overview if bucket["id"] == "deepseek")
    assert deepseek["provider"]["status"] == "ok"
    assert deepseek["provider"]["windows"] == provider_cards["deepseek"]["windows"]
    assert deepseek["provider"]["balances"] == provider_cards["deepseek"]["balances"]


def test_provider_overview_availability_row_tracks_provider_state(
    isolated_plugin_api, monkeypatch
):
    """Each bucket exposes a provider-level availability row so a provider
    with no assigned profile is still visible and actionable in the UI."""
    api = isolated_plugin_api
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: [])
    provider_cards = {
        "deepseek": {
            "id": "deepseek",
            "label": "DeepSeek",
            "status": "ok",
            "plan": None,
            "windows": [],
            "balances": [],
            "notice": None,
        }
    }

    overview = api._build_provider_overview(
        api._build_model_cards(),
        api._current_providers(),
        provider_cards,
    )

    deepseek = next(bucket for bucket in overview if bucket["id"] == "deepseek")
    assert deepseek["provider_availability"] == {
        "status": "ready",
        "status_label": "Ready",
    }

    # With no provider card at all (no credentials / fetch failure) the
    # availability row must read Not configured, never a fabricated status.
    overview = api._build_provider_overview(
        api._build_model_cards(),
        api._current_providers(),
        {},
    )
    deepseek = next(bucket for bucket in overview if bucket["id"] == "deepseek")
    assert deepseek["provider_availability"] == {
        "status": "unconfigured",
        "status_label": "Not configured",
    }


def test_provider_overview_availability_reflects_profile_status(
    isolated_plugin_api, monkeypatch
):
    """A rate-limited profile must surface on the provider availability row
    even when the provider usage API still answers (Codex Plus case)."""
    api = isolated_plugin_api
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: [])
    provider_cards = {
        "openai-codex": {
            "id": "openai-codex",
            "label": "ChatGPT or Codex Subscription",
            "status": "ok",
            "plan": "Plus",
            "windows": [],
            "balances": [],
            "notice": None,
        }
    }

    profile_cards = [
        {
            "id": "default",
            "profile": "default",
            "model": "gpt-5.6-luna",
            "provider": "openai-codex",
            "status": "rate_limited",
            "status_label": "Rate limited",
            "reset_at": "2026-09-02T01:04:00+00:00",
        },
        {
            "id": "worker-2",
            "profile": "worker-2",
            "model": "gpt-5.6-luna",
            "provider": "openai-codex",
            "status": "ready",
            "status_label": "Ready",
            "reset_at": None,
        },
    ]

    overview = api._build_provider_overview(
        profile_cards,
        api._current_providers(),
        provider_cards,
    )

    codex = next(bucket for bucket in overview if bucket["id"] == "openai-codex")
    assert codex["provider_availability"] == {
        "status": "rate_limited",
        "status_label": "Rate limited",
    }
    # The quota snapshot itself stays untouched: the availability row is
    # an aggregate, not a rewrite of the provider card.
    assert codex["provider"]["status"] == "ok"
    # The availability row carries the earliest profile reset so the
    # operator sees recovery time without opening every profile.
    assert codex["reset_at"] == "2026-09-02T01:04:00+00:00"


def test_provider_overview_marks_configured_without_quota(
    isolated_plugin_api, monkeypatch, tmp_path
):
    """A provider with Hermes credentials but no quota adapter is flagged
    configured so the UI can auto-hide it instead of skipping it."""
    api = isolated_plugin_api
    monkeypatch.setattr(api.runtime, "list_catalog_providers", lambda: [
        {"slug": "deepseek", "label": "DeepSeek"},
        {"slug": "openai-codex", "label": "ChatGPT or Codex Subscription"},
        {"slug": "minimax", "label": "MiniMax"},
        {"slug": "copilot", "label": "GitHub Copilot"},
        {"slug": "novita", "label": "NovitaAI"},
    ])
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: [
        {"name": "default", "path": tmp_path, "model": "m", "provider": "openai-codex"},
    ])
    monkeypatch.setattr(
        api.runtime,
        "read_auth_store",
        lambda path: {
            "providers": {"openai-codex": {"logged_in": True}},
            "credential_pool": {"copilot": [{"id": "c1", "access_token": "x"}]},
        },
    )
    profile_cards = [
        {
            "id": "default",
            "profile": "default",
            "model": "m",
            "provider": "openai-codex",
            "status": "ready",
            "status_label": "Ready",
            "reset_at": None,
        },
    ]

    overview = api._build_provider_overview(
        profile_cards,
        api._current_providers(),
        {},
    )

    codex = next(bucket for bucket in overview if bucket["id"] == "openai-codex")
    assert codex["configured"] is True
    assert codex["has_quota"] is True  # built-in quota adapter still applies

    copilot = next(bucket for bucket in overview if bucket["id"] == "copilot")
    assert copilot["configured"] is True
    assert copilot["has_quota"] is False

    # A catalog provider with no credentials anywhere stays unconfigured.
    novita = next(bucket for bucket in overview if bucket["id"] == "novita")
    assert novita["configured"] is False
    assert novita["has_quota"] is False


def test_provider_overview_never_leaks_credentials(
    isolated_plugin_api, monkeypatch
):
    api = isolated_plugin_api
    rows = [
        {"name": "worker-1", "path": Path("/tmp/a"), "model": "m", "provider": "deepseek"},
    ]
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: rows)
    monkeypatch.setattr(api.runtime, "resolve_provider_credentials", lambda provider: None)
    monkeypatch.setattr(api.runtime, "codex_configured", lambda: False)
    # Even if a stray credential value sneaks into the auth store, the
    # provider overview must never echo it back to the browser.
    monkeypatch.setattr(
        api.runtime,
        "read_auth_store",
        lambda _path: {
            "providers": {"deepseek": {"api_key": "SECRET-LEAK-VALUE"}},
            "credential_pool": {
                "deepseek": [
                    {
                        "id": "x",
                        "api_key": "POOL-SECRET-LEAK",
                        "access_token": "POOL-TOKEN-LEAK",
                    },
                ],
            },
        },
    )

    summary = api._cached_summary()
    blob = json.dumps(summary)
    assert "SECRET-LEAK-VALUE" not in blob
    assert "POOL-SECRET-LEAK" not in blob
    assert "POOL-TOKEN-LEAK" not in blob
