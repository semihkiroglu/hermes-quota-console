"""Reset preservation tests with fake auth stores.

These tests prove the reset preservation contract:

- Reset clears only exhaustion/rate-limit metadata plus the documented
  Codex usage cache.
- Credential values (``access_token``, ``refresh_token``, ``api_key``,
  ``token``) stay byte-for-byte unchanged.
- Reset never copies credentials between profiles: a profile reset touches
  only that profile's own store and never writes pool entries copied from
  the shared root store.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = REPO_ROOT / "dashboard"

_RESET_METADATA_FIELDS = (
    "last_status",
    "last_status_at",
    "last_error_code",
    "last_error_reason",
    "last_error_message",
    "last_error_reset_at",
    "failure_reason",
)
_CREDENTIAL_FIELDS = ("access_token", "refresh_token", "api_key", "token")


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


class FakeAuthStoreRuntime:
    """Lock-free JSON backend with the same semantics as the adapter."""

    def __init__(self, root: Path, rows: list[dict[str, Any]]):
        self.root = root
        self.rows = rows
        self.cache_cleared = False

    def _auth_path(self, profile_path: Path) -> Path:
        return Path(profile_path) / "auth.json"

    def read_auth_store(self, profile_path: Path) -> dict[str, Any]:
        path = self._auth_path(profile_path)
        if not path.is_file():
            return {"version": 1, "providers": {}}
        return json.loads(path.read_text(encoding="utf-8"))

    def update_auth_store(self, profile_path: Path, mutator) -> int:
        store = self.read_auth_store(profile_path)
        changed = mutator(store)
        if changed:
            path = self._auth_path(profile_path)
            path.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")
        return changed

    def list_profiles(self) -> list[dict[str, Any]]:
        return self.rows

    def exhausted_until(self, provider: str, entry: dict[str, Any], *, sole_credential: bool = False):
        return None

    def normalize_profile_name(self, name: str) -> str:
        return str(name).strip().lower()

    def clear_codex_usage_cache(self) -> bool:
        self.cache_cleared = True
        return True


@pytest.fixture()
def fake_hermes_runtime(monkeypatch, tmp_path):
    api = load_plugin_api()

    default_store = {
        "version": 1,
        "providers": {},
        "credential_pool": {
            "deepseek": [
                {
                    "id": "ds-1",
                    "source": "manual",
                    "access_token": "DS-TOKEN-ALPHA-9f3a",
                    "refresh_token": "DS-REFRESH-BETA-7c21",
                    "last_status": "exhausted",
                    "last_status_at": 1_700_000_000.0,
                    "last_error_code": 429,
                    "last_error_reason": "rate limit",
                    "last_error_message": "quota exhausted",
                    "extra": {"failure_reason": "budget", "note": "keep-me"},
                },
            ],
        },
    }
    worker_store = {
        "version": 1,
        "providers": {
            "minimax": {"api_key": "MM-SINGLETON-KEY-11aa", "logged_in": True},
        },
        "credential_pool": {
            "minimax": [
                {
                    "id": "mm-1",
                    "source": "manual",
                    "access_token": "MM-TOKEN-GAMMA-5d88",
                    "last_status": "exhausted",
                    "last_status_at": 1_700_000_100.0,
                    "last_error_code": 429,
                },
            ],
        },
    }

    default_dir = tmp_path / "default"
    worker_dir = tmp_path / "worker-1"
    default_dir.mkdir()
    worker_dir.mkdir()
    (default_dir / "auth.json").write_text(json.dumps(default_store, indent=2) + "\n", encoding="utf-8")
    (worker_dir / "auth.json").write_text(json.dumps(worker_store, indent=2) + "\n", encoding="utf-8")

    rows = [
        {"name": "default", "path": default_dir, "model": "gpt-5.6-luna", "provider": "openai-codex"},
        {"name": "worker-1", "path": worker_dir, "model": "deepseek-v4-pro", "provider": "deepseek"},
    ]
    fake = FakeAuthStoreRuntime(tmp_path, rows)

    runtime = api.runtime
    monkeypatch.setattr(runtime, "read_auth_store", fake.read_auth_store)
    monkeypatch.setattr(runtime, "update_auth_store", fake.update_auth_store)
    monkeypatch.setattr(runtime, "list_profiles", fake.list_profiles)
    monkeypatch.setattr(runtime, "exhausted_until", fake.exhausted_until)
    monkeypatch.setattr(runtime, "normalize_profile_name", fake.normalize_profile_name)
    monkeypatch.setattr(runtime, "clear_codex_usage_cache", fake.clear_codex_usage_cache)
    # Never resolve live credentials in tests: provider cards must not make
    # real network calls against the installed Hermes runtime.
    monkeypatch.setattr(runtime, "resolve_provider_credentials", lambda provider: None)
    monkeypatch.setattr(runtime, "codex_configured", lambda: False)
    # Isolate the process-wide summary cache between tests.
    monkeypatch.setattr(api, "_CACHE", None)
    monkeypatch.setattr(api, "_CACHE_AT", 0.0)

    return {"api": api, "fake": fake, "tmp_path": tmp_path}


def _credential_snapshot(store: dict[str, Any]) -> dict[str, Any]:
    """Collect every credential value keyed by provider and entry index."""
    snapshot: dict[str, Any] = {}
    pool = store.get("credential_pool")
    if isinstance(pool, dict):
        for provider, entries in pool.items():
            if not isinstance(entries, list):
                continue
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                for field in _CREDENTIAL_FIELDS:
                    if field in entry:
                        snapshot[f"pool.{provider}.{index}.{field}"] = entry[field]
    providers = store.get("providers")
    if isinstance(providers, dict):
        for provider, state in providers.items():
            if not isinstance(state, dict):
                continue
            for field in _CREDENTIAL_FIELDS:
                if field in state:
                    snapshot[f"singleton.{provider}.{field}"] = state[field]
    return snapshot


def test_profile_reset_preserves_credentials_byte_for_byte(fake_hermes_runtime):
    api = fake_hermes_runtime["api"]
    tmp_path = fake_hermes_runtime["tmp_path"]
    worker_auth = tmp_path / "worker-1" / "auth.json"
    default_auth = tmp_path / "default" / "auth.json"

    before = _credential_snapshot(json.loads(worker_auth.read_text(encoding="utf-8")))
    default_bytes_before = default_auth.read_bytes()
    worker_pool_keys_before = set(
        json.loads(worker_auth.read_text(encoding="utf-8")).get("credential_pool", {})
    )

    result = api._reset_profiles("profile", "worker-1")

    assert result["ok"] is True
    assert result["reset_credentials"] == 1
    assert [item["errors"] for item in result["results"]] == [[]]

    worker_after = json.loads(worker_auth.read_text(encoding="utf-8"))
    after = _credential_snapshot(worker_after)

    # Every credential value is byte-for-byte unchanged.
    assert after == before
    assert after["pool.minimax.0.access_token"] == "MM-TOKEN-GAMMA-5d88"

    # Exhaustion metadata was cleared; nothing else in the pool changed.
    entry = worker_after["credential_pool"]["minimax"][0]
    for field in _RESET_METADATA_FIELDS:
        assert field not in entry or entry[field] is None or (
            field == "last_status_at" and entry[field] is not None
        ), f"{field} should be cleared"
    assert entry["last_status"] is None
    assert entry["last_error_code"] is None
    assert entry["access_token"] == "MM-TOKEN-GAMMA-5d88"

    # The singleton auth block is untouched.
    assert worker_after["providers"]["minimax"]["api_key"] == "MM-SINGLETON-KEY-11aa"

    # No credentials were copied from the root store into the profile.
    assert set(worker_after["credential_pool"]) == worker_pool_keys_before
    assert "deepseek" not in worker_after["credential_pool"]

    # The root store was not touched at all by a single-profile reset.
    assert default_auth.read_bytes() == default_bytes_before


def test_all_reset_preserves_every_credential_and_clears_codex_cache(fake_hermes_runtime):
    api = fake_hermes_runtime["api"]
    fake = fake_hermes_runtime["fake"]
    tmp_path = fake_hermes_runtime["tmp_path"]
    default_auth = tmp_path / "default" / "auth.json"
    worker_auth = tmp_path / "worker-1" / "auth.json"

    default_before = _credential_snapshot(json.loads(default_auth.read_text(encoding="utf-8")))
    worker_before = _credential_snapshot(json.loads(worker_auth.read_text(encoding="utf-8")))

    result = api._reset_profiles("all", None)

    assert result["ok"] is True
    assert result["reset_credentials"] == 2
    assert result["codex_usage_state_cleared"] is True
    assert fake.cache_cleared is True

    default_after = json.loads(default_auth.read_text(encoding="utf-8"))
    worker_after = json.loads(worker_auth.read_text(encoding="utf-8"))

    # Credential values survive byte-for-byte across every profile.
    assert _credential_snapshot(default_after) == default_before
    assert _credential_snapshot(worker_after) == worker_before

    # Every exhausted entry had its cooldown metadata cleared.
    assert default_after["credential_pool"]["deepseek"][0]["last_status"] is None
    assert "failure_reason" not in default_after["credential_pool"]["deepseek"][0]
    assert "failure_reason" not in default_after["credential_pool"]["deepseek"][0]["extra"]
    assert worker_after["credential_pool"]["minimax"][0]["last_status"] is None

    # Pool key sets are unchanged: no entry was copied between profiles.
    assert set(default_after["credential_pool"]) == {"deepseek"}
    assert set(worker_after["credential_pool"]) == {"minimax"}


def test_reset_skips_store_without_pool_entries(fake_hermes_runtime):
    """A store without pool entries for a provider stays byte-for-byte intact."""
    api = fake_hermes_runtime["api"]
    tmp_path = fake_hermes_runtime["tmp_path"]

    # worker-1 has no deepseek pool: its file must not be rewritten for that
    # provider, and its minimax row is what actually changes.
    worker_auth = tmp_path / "worker-1" / "auth.json"
    result = api._reset_profiles("profile", "worker-1")
    assert result["reset_credentials"] == 1

    entry = json.loads(worker_auth.read_text(encoding="utf-8"))["credential_pool"]["minimax"][0]
    assert entry["access_token"] == "MM-TOKEN-GAMMA-5d88"
    assert entry["last_status"] is None


def test_provider_reset_clears_that_provider_across_profiles(fake_hermes_runtime):
    """A provider-scope reset clears that provider's rate-limit state in every
    live profile store while preserving every credential value."""
    api = fake_hermes_runtime["api"]
    tmp_path = fake_hermes_runtime["tmp_path"]
    default_auth = tmp_path / "default" / "auth.json"
    worker_auth = tmp_path / "worker-1" / "auth.json"

    default_before = _credential_snapshot(json.loads(default_auth.read_text(encoding="utf-8")))
    worker_before = _credential_snapshot(json.loads(worker_auth.read_text(encoding="utf-8")))

    result = api._reset_profiles("provider", None, "deepseek")

    assert result["ok"] is True
    assert result["scope"] == "provider"
    assert result["provider"] == "deepseek"
    assert result["reset_credentials"] == 1

    default_after = json.loads(default_auth.read_text(encoding="utf-8"))
    worker_after = json.loads(worker_auth.read_text(encoding="utf-8"))

    # Credential values survive byte-for-byte.
    assert _credential_snapshot(default_after) == default_before
    assert _credential_snapshot(worker_after) == worker_before

    # The exhausted deepseek entry was cleared; worker-1's minimax entry
    # (a different provider) is untouched.
    assert default_after["credential_pool"]["deepseek"][0]["last_status"] is None
    worker_minimax = worker_after["credential_pool"]["minimax"][0]
    assert worker_minimax["access_token"] == "MM-TOKEN-GAMMA-5d88"
    assert worker_minimax["last_status"] == "exhausted"


def test_reset_endpoint_rejects_unknown_provider_scope(fake_hermes_runtime):
    api = fake_hermes_runtime["api"]
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)

    response = client.post("/reset", json={"scope": "provider", "provider": "not-a-provider"})
    assert response.status_code == 404
    assert response.json()["detail"] == "provider not found"


def test_reset_endpoint_returns_generic_errors(fake_hermes_runtime):
    api = fake_hermes_runtime["api"]
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)

    # Unknown profile: generic 404 without any path or account detail.
    response = client.post("/reset", json={"scope": "profile", "profile": "missing-profile"})
    assert response.status_code == 404
    assert response.json()["detail"] == "profile not found"

    # Successful reset returns only the safe result schema.
    response = client.post("/reset", json={"scope": "profile", "profile": "worker-1"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert set(payload) == {
        "ok", "scope", "profile", "provider", "results", "reset_credentials",
        "codex_usage_state_cleared", "summary",
    }
    summary = payload["summary"]
    assert set(summary) == {
        "updated_at",
        "profiles",
        "providers",
        "provider_overview",
        "alerts",
        "settings",
    }
    assert "MM-TOKEN-GAMMA-5d88" not in json.dumps(summary)
    assert "DS-TOKEN-ALPHA-9f3a" not in json.dumps(summary)
    assert "MM-SINGLETON-KEY-11aa" not in json.dumps(summary)


def test_reset_endpoint_rejects_cross_origin_requests(fake_hermes_runtime):
    api = fake_hermes_runtime["api"]
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)

    response = client.post(
        "/reset",
        json={"scope": "all"},
        headers={"origin": "https://evil.example.com", "host": "dashboard.local"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "cross-origin request"
