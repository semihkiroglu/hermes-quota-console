"""Settings endpoint tests for the operator settings layer.

Covers the FastAPI surface added at ``/api/plugins/quota-console/settings``
(GET + PUT) and the summary enrichment that carries effective settings into
each provider bucket so the dashboard never has to compute them on the
client. The summary cache invalidation guarantees that PUTs reach the next
read without a process restart.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_API_PATH = REPO_ROOT / "dashboard" / "plugin_api.py"


@pytest.fixture
def isolated_plugin_api(monkeypatch, tmp_path):
    module_name = f"quota_console_settings_api_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_API_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        for key in list(sys.modules):
            if key == module_name or key.startswith(f"{module_name}."):
                sys.modules.pop(key, None)
        raise

    # Pin storage to a tmp_path so tests own the disk file. Drop any cached
    # settings state (the module holds no module-level mutable state but the
    # cache under test is plugin_api._CACHE).
    monkeypatch.setattr(module._settings, "_storage_dir", lambda: tmp_path)
    monkeypatch.setattr(module._settings, "storage_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(module, "_CACHE", None)
    monkeypatch.setattr(module, "_CACHE_AT", 0.0)
    # Stable profile list: no live profiles, no remote calls.
    monkeypatch.setattr(module.runtime, "list_profiles", lambda: [])
    monkeypatch.setattr(module.runtime, "resolve_provider_credentials", lambda provider: None)
    monkeypatch.setattr(module.runtime, "codex_configured", lambda: False)
    monkeypatch.setattr(module.runtime, "list_catalog_providers", lambda: [])
    try:
        yield module
    finally:
        for key in list(sys.modules):
            if key == module_name or key.startswith(f"{module_name}."):
                sys.modules.pop(key, None)


def _client(api):
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET endpoint
# ---------------------------------------------------------------------------


def test_settings_get_returns_defaults_and_effective(isolated_plugin_api):
    api = isolated_plugin_api
    client = _client(api)
    response = client.get("/settings")
    assert response.status_code == 200
    body = response.json()
    assert body["defaults"] == {}
    assert body["providers"] == {}
    assert "effective" in body
    assert set(body["fields"]) == {
        "window_low_percent",
        "balance_low_amount",
        "balance_exhausted_at_zero",
        "note",
    }
    assert body["schema"]["note_max_length"] == 120
    assert body["storage_path"].endswith("config.json")


def test_settings_get_reflects_disk_state(isolated_plugin_api):
    api = isolated_plugin_api
    api._settings.save({
        "defaults": {"window_low_percent": 25},
        "providers": {"deepseek": {"note": "prod"}},
    })
    body = _client(api).get("/settings").json()
    assert body["defaults"]["window_low_percent"] == 25
    assert body["providers"]["deepseek"]["note"] == "prod"
    # Effective view per known provider carries the merge.
    assert body["effective"]["deepseek"]["window_low_percent"] == 25
    assert body["effective"]["deepseek"]["note"] == "prod"


# ---------------------------------------------------------------------------
# PUT endpoint
# ---------------------------------------------------------------------------


def test_settings_put_persists_payload_and_invalidates_cache(isolated_plugin_api):
    api = isolated_plugin_api
    api._CACHE = {"cached": "stale"}  # ensure PUT invalidates
    client = _client(api)
    response = client.put(
        "/settings",
        json={
            "defaults": {"window_low_percent": 30},
            "providers": {"deepseek": {"note": "prod key"}},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["defaults"]["window_low_percent"] == 30
    assert body["providers"]["deepseek"]["note"] == "prod key"
    assert api._CACHE is None
    # Confirm the file is on disk and re-readable
    raw = json.loads(api._settings.storage_path().read_text(encoding="utf-8"))
    assert raw["defaults"]["window_low_percent"] == 30


def test_settings_put_rejects_unknown_top_level_key(isolated_plugin_api):
    api = isolated_plugin_api
    client = _client(api)
    response = client.put("/settings", json={"unknown": True})
    assert response.status_code == 400
    assert "unknown" in response.json()["detail"]
    # Nothing was written to disk.
    assert not api._settings.storage_path().exists()


def test_settings_put_rejects_invalid_threshold_range(isolated_plugin_api):
    api = isolated_plugin_api
    client = _client(api)
    response = client.put("/settings", json={"defaults": {"window_low_percent": 150}})
    assert response.status_code == 400
    assert "1..100" in response.json()["detail"]


def test_settings_put_rejects_multiline_note(isolated_plugin_api):
    api = isolated_plugin_api
    client = _client(api)
    response = client.put(
        "/settings", json={"providers": {"deepseek": {"note": "first\nsecond"}}}
    )
    assert response.status_code == 400
    assert "single line" in response.json()["detail"]


def test_settings_put_rejects_oversized_note(isolated_plugin_api):
    api = isolated_plugin_api
    client = _client(api)
    response = client.put(
        "/settings", json={"providers": {"deepseek": {"note": "x" * 121}}}
    )
    assert response.status_code == 400


def test_settings_put_rejects_invalid_provider_id(isolated_plugin_api):
    api = isolated_plugin_api
    client = _client(api)
    response = client.put("/settings", json={"providers": {"Bad ID": {}}})
    assert response.status_code == 400


def test_settings_put_rejects_cross_origin(isolated_plugin_api):
    api = isolated_plugin_api
    client = _client(api)
    response = client.put(
        "/settings",
        json={"defaults": {}, "providers": {}},
        headers={"origin": "https://evil.example.com", "host": "dashboard.local"},
    )
    assert response.status_code == 403
    assert not api._settings.storage_path().exists()


# ---------------------------------------------------------------------------
# Summary integration
# ---------------------------------------------------------------------------


def test_summary_includes_settings_and_carries_effective_view(isolated_plugin_api):
    api = isolated_plugin_api
    # Seed settings so we can see the merge in the summary.
    api._settings.save({
        "defaults": {"window_low_percent": 25},
        "providers": {"deepseek": {"note": "prod"}},
    })
    summary = api._cached_summary()
    assert "settings" in summary
    assert summary["settings"]["providers"]["deepseek"]["note"] == "prod"

    deepseek_bucket = next(
        bucket for bucket in summary["provider_overview"] if bucket["id"] == "deepseek"
    )
    effective = deepseek_bucket["settings"]
    # Note override wins for the note; window_low_percent falls back to global.
    assert effective["note"] == "prod"
    assert effective["window_low_percent"] == 25
    assert effective["balance_exhausted_at_zero"] is None


def test_summary_settings_block_has_no_credentials(isolated_plugin_api):
    api = isolated_plugin_api
    api._settings.save({
        "defaults": {},
        "providers": {"deepseek": {"note": "secret note"}},
    })
    summary = api._cached_summary()
    blob = json.dumps(summary)
    # The summary must not echo back any of the standard secret keys, even
    # though the plugin never reads credentials in the first place — this is
    # a regression guard against future "let's just include more context"
    # drift.
    for forbidden in (
        "api_key",
        "access_token",
        "refresh_token",
        "sk-proj-",
        "Bearer ",
    ):
        assert forbidden not in blob or forbidden == "secret note" and "secret note" not in blob


def test_settings_persist_across_module_reload(isolated_plugin_api, tmp_path):
    api = isolated_plugin_api
    api._settings.save({
        "defaults": {"window_low_percent": 20},
        "providers": {"deepseek": {"note": "prod"}},
    })
    saved_path = api._settings.storage_path()
    # Simulate a process restart by reloading the settings module from
    # disk. The new instance must read the same payload: the file lives
    # outside the install tree, where it survives ``hermes plugins
    # remove`` / ``update``.
    spec = importlib.util.spec_from_file_location(
        f"quota_console_settings_reload_{uuid.uuid4().hex}",
        REPO_ROOT / "dashboard" / "settings.py",
    )
    assert spec is not None and spec.loader is not None
    reloaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = reloaded
    # First execute the module so its source defines ``storage_path`` /
    # ``_storage_dir``; then monkeypatch them so the new module reads from
    # the same temp directory the save went to.
    spec.loader.exec_module(reloaded)
    reloaded._storage_dir = lambda: tmp_path
    reloaded.storage_path = lambda: saved_path
    final = reloaded.load_raw()
    assert final["defaults"]["window_low_percent"] == 20
    assert final["providers"]["deepseek"]["note"] == "prod"
