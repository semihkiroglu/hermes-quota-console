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
