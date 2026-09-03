"""Tests for the plugin version helper backing the footer version chip.

The summary carries the same version string that pyproject.toml declares
so the dashboard footer can show what the release workflow tags. The
reader must fail closed (return ``None``) when the project file is
missing or malformed — the UI then hides the chip instead of crashing.
"""

from __future__ import annotations

import pytest


def test_version_read_from_pyproject(plugin_api):
    version = plugin_api._plugin_version()
    assert version is not None
    # The test repository's own pyproject.toml is the source of truth.
    assert version.count(".") >= 1


def test_version_missing_pyproject_returns_none(plugin_api, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_api, "_DASHBOARD_DIR", tmp_path)
    assert plugin_api._plugin_version() is None


def test_version_malformed_pyproject_returns_none(plugin_api, tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"x\"\n# version line missing\n", encoding="utf-8"
    )
    monkeypatch.setattr(plugin_api, "_DASHBOARD_DIR", tmp_path)
    assert plugin_api._plugin_version() is None


def test_summary_carries_version(plugin_api):
    summary = plugin_api._build_summary()
    assert "version" in summary
    assert summary["version"] == plugin_api._plugin_version()


def test_latest_release_fetches_and_caches(plugin_api, monkeypatch):
    calls = {"count": 0}

    class FakeResponse:
        def __init__(self, tag):
            self._tag = tag

        def raise_for_status(self):
            return None

        def json(self):
            return {"tag_name": self._tag}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            calls["count"] += 1
            return FakeResponse("v0.1.2")

    monkeypatch.setattr(plugin_api, "httpx", type("H", (), {"Client": FakeClient,
                                                           "HTTPError": Exception}))
    monkeypatch.setattr(plugin_api, "_LATEST_RELEASE_CACHE", {"at": 0.0, "value": None})

    assert plugin_api._latest_release() == "v0.1.2"
    # Second call within TTL must hit the cache, not the network.
    assert plugin_api._latest_release() == "v0.1.2"
    assert calls["count"] == 1


def test_latest_release_fails_closed(plugin_api, monkeypatch):
    class FailingClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            raise RuntimeError("network down")

    monkeypatch.setattr(plugin_api, "httpx", type("H", (), {"Client": FailingClient,
                                                           "HTTPError": Exception}))
    monkeypatch.setattr(plugin_api, "_LATEST_RELEASE_CACHE", {"at": 0.0, "value": None})
    assert plugin_api._latest_release() is None


def test_latest_release_malformed_payload_fails_closed(plugin_api, monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"not_a_tag": True}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(plugin_api, "httpx", type("H", (), {"Client": FakeClient,
                                                           "HTTPError": Exception}))
    monkeypatch.setattr(plugin_api, "_LATEST_RELEASE_CACHE", {"at": 0.0, "value": None})
    assert plugin_api._latest_release() is None
