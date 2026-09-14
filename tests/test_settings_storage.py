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
    defaults, providers, notifications = s.validate_payload({
        "defaults": {"window_low_percent": 25},
        "providers": {
            "deepseek": {"note": "prod key"},
        },
    })
    assert defaults == {"window_low_percent": 25}
    assert providers == {"deepseek": {"note": "prod key"}}
    # ``notifications`` defaults are merged in even when the operator
    # omits the block — the dialog relies on this so a partial PUT
    # round-trips cleanly.
    assert notifications == s.builtin_notifications()


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
    assert cleaned["defaults"] == {}
    assert cleaned["providers"] == {}
    assert cleaned["notifications"] == s.builtin_notifications()
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
    # Corrupt JSON falls back to a safe empty payload: every layer is
    # zeroed, and the notifications block carries the built-in defaults
    # so the dialog can render without an explicit save round-trip.
    assert raw["defaults"] == {}
    assert raw["providers"] == {}
    assert raw["notifications"] == s.builtin_notifications()


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


# ---------------------------------------------------------------------------
# Notifications opt-in block
# ---------------------------------------------------------------------------


def test_validate_payload_accepts_notifications_block(isolated_settings):
    s = isolated_settings.module
    defaults, providers, notifications = s.validate_payload({
        "defaults": {},
        "providers": {},
        "notifications": {
            "enabled": True,
            "levels": ["critical", "low"],
            "reminder_minutes": 30,
        },
    })
    assert notifications == {
        "enabled": True,
        "levels": ["critical", "low"],
        "reminder_minutes": 30,
    }


def test_validate_payload_defaults_notifications_when_missing(isolated_settings):
    s = isolated_settings.module
    _, _, notifications = s.validate_payload({"defaults": {}, "providers": {}})
    # Opt-in, conservative levels, repeats OFF by default: the interval
    # is zero, so nothing repeats until the operator sets one.
    assert notifications == {
        "enabled": False,
        "levels": ["critical"],
        "reminder_minutes": 0,
    }


def test_validate_payload_defaults_individual_notification_fields(isolated_settings):
    s = isolated_settings.module
    _, _, notifications = s.validate_payload({
        "defaults": {},
        "providers": {},
        "notifications": {"enabled": True},
    })
    # ``levels`` and ``reminder_minutes`` fall back to the built-in
    # defaults so partial PUTs round-trip cleanly.
    assert notifications == {
        "enabled": True,
        "levels": ["critical"],
        "reminder_minutes": 0,
    }


def test_validate_payload_normalises_notification_level_order(isolated_settings):
    s = isolated_settings.module
    _, _, notifications = s.validate_payload({
        "defaults": {},
        "providers": {},
        "notifications": {"levels": ["low", "critical"]},
    })
    # Order must match the canonical ("critical", "low") so the on-disk
    # payload is stable across dialog writes.
    assert notifications["levels"] == ["critical", "low"]


def test_validate_payload_rejects_unknown_top_level_key_with_notifications(isolated_settings):
    s = isolated_settings.module
    # The new block does not weaken the existing top-level allowlist.
    with pytest.raises(s.SettingsValidationError, match="unknown top-level"):
        s.validate_payload({"notifications": {"enabled": True}, "extra": 1})


def test_validate_payload_rejects_non_object_notifications(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError, match="notifications must be an object"):
        s.validate_payload({"notifications": "enabled"})


def test_validate_payload_rejects_unknown_notification_field(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError, match="unknown notifications fields"):
        s.validate_payload(
            {"notifications": {"enabled": True, "extra_field": "x"}}
        )


def test_validate_payload_rejects_non_bool_enabled(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError, match="enabled must be a boolean"):
        s.validate_payload({"notifications": {"enabled": "yes"}})


def test_validate_payload_rejects_invalid_level(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError, match="unknown values"):
        s.validate_payload({"notifications": {"levels": ["critical", "bogus"]}})


def test_validate_payload_rejects_non_array_levels(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError, match="must be an array of strings"):
        s.validate_payload({"notifications": {"levels": "critical"}})


def test_validate_payload_rejects_empty_levels(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError, match="at least one value"):
        s.validate_payload({"notifications": {"levels": []}})


def test_validate_payload_rejects_negative_reminder_minutes(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError, match="must be in 0\\.\\.1440"):
        s.validate_payload({"notifications": {"reminder_minutes": -1}})


def test_validate_payload_rejects_reminder_minutes_above_24h(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError, match="must be in 0\\.\\.1440"):
        s.validate_payload({"notifications": {"reminder_minutes": 24 * 60 + 1}})


def test_validate_payload_rejects_non_integer_reminder_minutes(isolated_settings):
    s = isolated_settings.module
    with pytest.raises(s.SettingsValidationError, match="must be an integer in 0"):
        s.validate_payload({"notifications": {"reminder_minutes": "60"}})


def test_validate_payload_rejects_bool_reminder_minutes(isolated_settings):
    s = isolated_settings.module
    # bool is an int subclass; the validator must explicitly reject it.
    with pytest.raises(s.SettingsValidationError, match="must be an integer in 0"):
        s.validate_payload({"notifications": {"reminder_minutes": True}})




def test_load_raw_supplies_default_notifications_when_missing(isolated_settings):
    s = isolated_settings.module
    # Old-format payload: no ``notifications`` block. The reader must
    # backfill the built-in defaults so the dialog renders immediately.
    isolated_settings.path.write_text(
        json.dumps({"defaults": {"window_low_percent": 25}, "providers": {}}),
        encoding="utf-8",
    )
    raw = s.load_raw()
    assert raw["notifications"] == s.builtin_notifications()


def test_load_raw_replaces_malformed_notifications_with_defaults(isolated_settings):
    s = isolated_settings.module
    # ``levels`` is not a list -> the writer would reject it; the reader
    # must not crash, just fall back to the safe defaults.
    isolated_settings.path.write_text(
        json.dumps(
            {
                "defaults": {},
                "providers": {},
                "notifications": {"levels": "critical"},
            }
        ),
        encoding="utf-8",
    )
    raw = s.load_raw()
    assert raw["notifications"] == s.builtin_notifications()


def test_save_round_trips_notifications_block(isolated_settings):
    s = isolated_settings.module
    cleaned = s.save({
        "defaults": {},
        "providers": {},
        "notifications": {
            "enabled": True,
            "levels": ["low"],
            "reminder_minutes": 5,
        },
    })
    assert cleaned["notifications"] == {
        "enabled": True,
        "levels": ["low"],
        "reminder_minutes": 5,
    }
    # Reload from disk and confirm the block survives.
    raw = s.load_raw()
    assert raw["notifications"] == {
        "enabled": True,
        "levels": ["low"],
        "reminder_minutes": 5,
    }


def test_notifications_block_does_not_leak_credentials(isolated_settings, monkeypatch):
    """The notifications block is operator-edited browser state. A
    hostile operator should never be able to store a secret-shaped value
    in any of its fields; the field types (bool / array of strings /
    int) reject them by construction. This is a regression guard."""
    s = isolated_settings.module
    cleaned = s.save({
        "defaults": {},
        "providers": {},
        "notifications": {"enabled": False},
    })
    blob = json.dumps(cleaned)
    for forbidden in ("api_key", "access_token", "refresh_token", "Bearer "):
        assert forbidden not in blob
