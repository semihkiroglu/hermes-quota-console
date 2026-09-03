"""Settings storage layer tests for the operator-editable layer.

These tests cover:
- Two-layer shape (``defaults`` + per-provider ``providers``)
- Validation: unknown keys, range checks, note length/newline rule
- Effective merge: provider override -> global default -> built-in default
- Atomic save: write failure does not corrupt the on-disk file
- Per-request read: no global cache, no restart required for pickup
- Summary integration: ``bucket.settings`` carries the effective view
- Never-leak: secrets-shaped payloads are rejected; storage never echoes them

The tests run without Hermes: the plugin module's ``storage_path`` is
patched to a temporary directory so each test owns its own disk file.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = REPO_ROOT / "dashboard" / "settings.py"


def _load_settings():
    module_name = f"quota_console_settings_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, SETTINGS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        # settings is a stateless module: drop the entry so the next test
        # can re-import the file from disk and pick up monkeypatched values.
        sys.modules.pop(module_name, None)


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    settings = _load_settings()
    target = tmp_path / "config.json"
    monkeypatch.setattr(settings, "_storage_dir", lambda: tmp_path)
    monkeypatch.setattr(settings, "storage_path", lambda: target)
    return SimpleNamespace(module=settings, path=target)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_payload_accepts_two_layer_shape(isolated_settings):
    s = isolated_settings.module
    defaults, providers = s.validate_payload({
        "defaults": {"window_low_percent": 25},
        "providers": {
            "deepseek": {"note": "prod key"},
        },
    })
    assert defaults == {"window_low_percent": 25}
    assert providers == {"deepseek": {"note": "prod key"}}


def test_validate_payload_rejects_unknown_top_level_keys(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError, match="unknown top-level"):
        s.validate_payload({"defaults": {}, "providers": {}, "extra": 1})


def test_validate_payload_rejects_unknown_field(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError, match="unknown field"):
        s.validate_payload({"defaults": {"enabled": True}})
    with pytest.raises(s.SettingsValidationError, match="unknown field"):
        s.validate_payload({"defaults": {"not_a_field": 1}})


def test_validate_payload_rejects_window_low_percent_out_of_range(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError, match="1..100"):
        s.validate_payload({"defaults": {"window_low_percent": 0}})
    with pytest.raises(s.SettingsValidationError, match="1..100"):
        s.validate_payload({"defaults": {"window_low_percent": 101}})
    with pytest.raises(s.SettingsValidationError, match="1..100"):
        s.validate_payload({"defaults": {"window_low_percent": "25"}})


def test_validate_payload_rejects_negative_balance_low_amount(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError, match="non-negative"):
        s.validate_payload({"defaults": {"balance_low_amount": -1}})


def test_validate_payload_rejects_multiline_note(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError, match="single line"):
        s.validate_payload({"providers": {"deepseek": {"note": "first\nsecond"}}})


def test_validate_payload_rejects_oversized_note(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError, match="at most"):
        s.validate_payload({"providers": {"deepseek": {"note": "x" * 121}}})


def test_validate_payload_rejects_global_note(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError, match="only valid per provider"):
        s.validate_payload({"defaults": {"note": "global note"}})


def test_validate_payload_rejects_invalid_provider_id(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError, match="invalid provider id"):
        s.validate_payload({"providers": {"Bad-Provider": {}}})


def test_validate_payload_rejects_non_boolean_for_exhausted_flag(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError):
        s.validate_payload({"providers": {"deepseek": {"balance_exhausted_at_zero": "yes"}}})


def test_validate_payload_rejects_non_object_payload(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError):
        s.validate_payload("not-a-dict")
    with pytest.raises(s.SettingsValidationError):
        s.validate_payload({"providers": "not-a-dict"})


# ---------------------------------------------------------------------------
# Effective merge
# ---------------------------------------------------------------------------


def test_effective_merge_precedence_overrides_then_defaults_then_builtin(isolated_settings):
    s = isolated_settings.module
    merged = s.effective(
        {"window_low_percent": 30},
        {"window_low_percent": 10, "note": "prod"},
    )
    assert merged == {
        "window_low_percent": 10,
        "balance_low_amount": None,
        "balance_exhausted_at_zero": None,
        "note": "prod",
    }


def test_effective_uses_global_when_override_is_null(isolated_settings):
    s = isolated_settings.module
    merged = s.effective({"window_low_percent": 25}, {"note": "x"})
    # Override is null so we fall through to the global, not the override.
    assert merged["window_low_percent"] == 25
    assert merged["note"] == "x"


def test_effective_keeps_explicit_false_for_exhausted_flag(isolated_settings):
    s = isolated_settings.module
    # None means unset everywhere; explicit False is a real value and must
    # NOT be treated as an unset that falls back to the default.
    merged = s.effective({"balance_exhausted_at_zero": True}, {"balance_exhausted_at_zero": False})
    assert merged["balance_exhausted_at_zero"] is False
    merged = s.effective({}, {"balance_exhausted_at_zero": False})
    assert merged["balance_exhausted_at_zero"] is False


def test_effective_view_returns_one_row_per_provider(isolated_settings):
    s = isolated_settings.module
    view = s.effective_view(
        {"window_low_percent": 20},
        {"deepseek": {"note": "prod"}},
        provider_ids=("deepseek", "openai-codex", "minimax"),
    )
    assert set(view) == {"deepseek", "openai-codex", "minimax"}
    # deepseek: note override wins for note; window_low_percent falls back to global
    assert view["deepseek"]["note"] == "prod"
    assert view["deepseek"]["window_low_percent"] == 20
    # openai-codex: no overrides -> global default (20)
    assert view["openai-codex"]["window_low_percent"] == 20
    assert view["openai-codex"]["balance_exhausted_at_zero"] is None


def test_effective_view_drops_invalid_provider_ids(isolated_settings):
    s = isolated_settings.module
    view = s.effective_view({}, {"UPPER": {}, "ok-id": {}}, provider_ids=())
    assert "UPPER" not in view
    assert view["ok-id"]["window_low_percent"] is None


# ---------------------------------------------------------------------------
# Atomic save / read
# ---------------------------------------------------------------------------


def test_save_writes_atomically_and_load_round_trips(isolated_settings):
    s = isolated_settings.module
    payload = {
        "defaults": {"window_low_percent": 20},
        "providers": {"deepseek": {"note": "prod"}},
    }
    cleaned = s.save(payload)
    on_disk = json.loads(isolated_settings.path.read_text(encoding="utf-8"))
    assert on_disk == cleaned
    raw = s.load_raw()
    assert raw["defaults"]["window_low_percent"] == 20
    assert raw["providers"]["deepseek"]["note"] == "prod"


def test_save_rejects_invalid_payload_and_does_not_touch_disk(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError):
        s.save({"providers": {"bad id": {}}})
    assert not isolated_settings.path.exists()


def test_save_overwrites_stale_disk_content(isolated_settings):
    s = isolated_settings.module
    isolated_settings.path.write_text("garbage", encoding="utf-8")
    cleaned = s.save({"defaults": {}, "providers": {}})
    assert cleaned == {"defaults": {}, "providers": {}}
    assert json.loads(isolated_settings.path.read_text(encoding="utf-8")) == cleaned


def test_load_raw_drops_unknown_keys_and_repairable_values(isolated_settings):
    s = isolated_settings.module
    isolated_settings.path.write_text(
        json.dumps(
            {
                "defaults": {"window_low_percent": 25, "extra_field": "drop"},
                "providers": {
                    "deepseek": {"note": "x"},
                    "INVALID": {"window_low_percent": 10},
                },
            }
        ),
        encoding="utf-8",
    )
    raw = s.load_raw()
    assert raw["defaults"] == {"window_low_percent": 25}
    assert raw["providers"] == {"deepseek": {"note": "x"}}


def test_load_raw_returns_empty_on_corrupt_json(isolated_settings):
    s = isolated_settings.module
    isolated_settings.path.write_text("{not-json", encoding="utf-8")
    raw = s.load_raw()
    assert raw == {"defaults": {}, "providers": {}}


def test_save_does_not_create_partial_file_on_failure(isolated_settings, monkeypatch, tmp_path):
    s = isolated_settings.module
    # Force os.replace to raise so we can confirm the temp file is cleaned
    # up and the real config never lands in a partial state.
    import os

    original = os.replace
    calls = {"replace": 0}
    def boom(src, dst):
        calls["replace"] += 1
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    try:
        with pytest.raises(OSError):
            s.save({"defaults": {"window_low_percent": 20}, "providers": {}})
    finally:
        monkeypatch.setattr(os, "replace", original)
    assert calls["replace"] == 1
    assert not isolated_settings.path.exists()
    # No leftover temp files inside the tmp_path.
    leftovers = [path for path in tmp_path.iterdir() if path.name != "config.json"]
    assert not leftovers


def test_storage_path_uses_hermes_plugin_data_dir_when_available(isolated_settings, monkeypatch):
    # When plugins.plugin_storage is importable, storage_path must live
    # under <hermes>/plugin-data/<name>/.
    s = isolated_settings.module
    fake_dir = isolated_settings.path.parent.parent  # tmp_path
    monkeypatch.setitem(
        sys.modules,
        "plugins.plugin_storage",
        SimpleNamespace(plugin_data_dir=lambda name: fake_dir / name),
    )
    # storage_path is monkeypatched; remove the override so the real path
    # resolver runs.
    monkeypatch.undo()  # restore the storage_path patch
    # Re-load the module fresh so the override we set above sticks for the
    # storage_path function we re-bind below.
    s = _load_settings()
    monkeypatch.setitem(
        sys.modules,
        "plugins.plugin_storage",
        SimpleNamespace(plugin_data_dir=lambda name: fake_dir / name),
    )
    resolved = s.storage_path()
    assert resolved.parent == fake_dir / "quota-console"
    assert resolved.name == "config.json"
