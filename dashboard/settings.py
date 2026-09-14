"""Operator-editable settings for the quota-console dashboard plugin.

Settings persist in a per-plugin JSON file outside the live install tree, so
``hermes plugins update`` and ``hermes plugins remove`` never touch operator
state. The storage location follows the hermes-achievements bundle precedent::

    <hermes_home>/plugin-data/quota-console/config.json

When Hermes' ``plugins.plugin_storage`` helper is importable we use
``plugin_data_dir("quota-console")`` directly; otherwise we fall back to the
same layout via ``hermes_constants.get_hermes_home()`` so the plugin keeps
working in standalone test environments.

The on-disk shape is three top-level blocks::

    {
      "defaults": {                  # global default layer
        "window_low_percent": null,
        "balance_low_amount": null,
        "balance_exhausted_at_zero": null
        # note is per-provider only; a global note is rejected
      },
      "providers": {                 # per-provider override layer
        "deepseek": {
          "window_low_percent": 15,
          "balance_low_amount": null,
          "balance_exhausted_at_zero": true,
          "note": "prod key"
        }
      },
      "notifications": {             # browser-side notification opt-in
        "enabled": false,            # off by default — opt-in
        "levels": ["critical"],      # which alert levels fire a notification
        "reminder_minutes": 0        # repeat cadence; zero switches repeats
                                     # off entirely
      }
    }

Effective value = provider override if set, else global default, else the
built-in default (which is ``None`` for every threshold). The notifications
block lives at the top level on purpose — it does not belong under
``defaults`` (global) or ``providers`` (per-provider mute is a 1.0.0 item).
The module never stores credential values, endpoint URLs, mapping paths, or
notification endpoint tokens — only the alert-threshold fields, the
per-provider note, and the (browser-controlled) opt-in flags.

Reads are per-request and lock-free; writes use a write-temp-then-os.replace
pattern under a process-local lock so a concurrent dashboard and gateway
update cannot interleave. Settings never reach the browser without first
being validated through :func:`validate_payload`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("hermes-quota-console.settings")

_PLUGIN_NAME = "quota-console"
_DATA_FILENAME = "config.json"

# Allowed keys are exactly the operator contract. Anything else is rejected on
# both read (silently dropped) and write (HTTP 400).
_PROVIDER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_NOTE_MAX_LEN = 120

_FIELDS: tuple[str, ...] = (
    "window_low_percent",
    "balance_low_amount",
    "balance_exhausted_at_zero",
    "note",
)

_BUILTIN_DEFAULTS: dict[str, Any] = {
    "window_low_percent": None,
    "balance_low_amount": None,
    "balance_exhausted_at_zero": None,
    "note": None,
}

# Notification opt-in block. Stored at the top level (not under ``defaults``
# or ``providers``) on purpose: it is browser-only state, not an alert
# threshold, and per-provider mute is a 1.0.0 item. ``levels`` is the alert
# taxonomy the backend already uses in ``summary.alerts``: ``critical``
# fires when the red top alert shows, ``low`` fires when the yellow one
# does. Operators enable only the severity they want — defaults are
# conservative so the dashboard never spams the operator on page load.
_NOTIFICATION_LEVELS: frozenset[str] = frozenset({"critical", "low"})
# Iteration order is explicitly pinned so the on-disk payload and the
# dialog state stay stable across runs. A frozenset iterates in
# insertion-hash order which can flip between Python builds; this tuple
# is the canonical level order.
_NOTIFICATION_LEVELS_ORDER: tuple[str, ...] = ("critical", "low")
_NOTIFICATION_LEVELS_DEFAULT: tuple[str, ...] = ("critical",)
# Repeat-reminder window. ``reminder_minutes`` is the gap between repeat
# notifications on the same still-active alert; zero switches repeats off
# entirely (a new alert notifies once and never repeats while it stays
# active). The min/max bracket keeps the dialog input sane.
_NOTIFICATION_REMINDER_MIN: int = 0
_NOTIFICATION_REMINDER_MAX: int = 24 * 60  # 24h cap keeps the input sane
_NOTIFICATION_REMINDER_DEFAULT: int = 0
_NOTIFICATIONS_ENABLED_DEFAULT: bool = False

_BUILTIN_NOTIFICATIONS: dict[str, Any] = {
    "enabled": _NOTIFICATIONS_ENABLED_DEFAULT,
    "levels": list(_NOTIFICATION_LEVELS_DEFAULT),
    "reminder_minutes": _NOTIFICATION_REMINDER_DEFAULT,
}

_WRITE_LOCK = threading.Lock()


class SettingsValidationError(ValueError):
    """Raised when a settings payload violates the operator contract."""

    def __init__(self, message: str, *, field: Optional[str] = None):
        super().__init__(message)
        self.field = field


# ---------------------------------------------------------------------------
# Storage location
# ---------------------------------------------------------------------------


def _storage_dir() -> Path:
    """Return (and create) the durable settings directory for this plugin."""
    try:
        from plugins.plugin_storage import plugin_data_dir  # type: ignore[import-not-found]

        return plugin_data_dir(_PLUGIN_NAME)
    except Exception:
        # Standalone mode (tests, hermes-agent absent): mirror the same
        # layout so the on-disk file lives in the canonical location.
        try:
            from hermes_constants import get_hermes_home
            root = Path(get_hermes_home()) / "plugin-data" / _PLUGIN_NAME
        except Exception:
            root = Path.home() / ".hermes" / "plugin-data" / _PLUGIN_NAME
        root.mkdir(parents=True, exist_ok=True)
        return root


def storage_path() -> Path:
    """Return the canonical settings file path.

    The file is created on first write; it is never touched on read.
    """
    return _storage_dir() / _DATA_FILENAME


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _valid_provider_id(provider_id: Any) -> bool:
    return isinstance(provider_id, str) and bool(_PROVIDER_ID_PATTERN.fullmatch(provider_id))


def _normalize_field(key: str, value: Any) -> Any:
    """Validate and normalize one field's value; raise on invalid input.

    ``None`` is allowed and means "unset" for every field. An explicit
    ``True``/``False`` is required for the boolean field.
    """
    if value is None:
        return None
    if key == "window_low_percent":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsValidationError(
                "window_low_percent must be an integer in 1..100", field=key
            )
        if value < 1 or value > 100:
            raise SettingsValidationError(
                "window_low_percent must be in 1..100", field=key
            )
        return value
    if key == "balance_low_amount":
        if isinstance(value, bool) or isinstance(value, int):
            value = float(value)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise SettingsValidationError(
                "balance_low_amount must be a non-negative number", field=key
            )
        numeric = float(value)
        if numeric < 0:
            raise SettingsValidationError(
                "balance_low_amount must be a non-negative number", field=key
            )
        # JSON-friendly roundtrip: keep ints as ints, floats as floats.
        if numeric.is_integer() and abs(numeric) < 1e15:
            return int(numeric)
        return numeric
    if key == "balance_exhausted_at_zero":
        if not isinstance(value, bool):
            raise SettingsValidationError(
                "balance_exhausted_at_zero must be a boolean", field=key
            )
        return value
    if key == "note":
        if not isinstance(value, str):
            raise SettingsValidationError("note must be a string", field=key)
        if "\n" in value or "\r" in value:
            raise SettingsValidationError(
                "note must be a single line", field=key
            )
        if len(value) > _NOTE_MAX_LEN:
            raise SettingsValidationError(
                f"note must be at most {_NOTE_MAX_LEN} characters", field=key
            )
        return value
    raise SettingsValidationError(f"unknown field: {key}", field=key)


def _normalize_layer(layer: Any, *, layer_name: str) -> dict[str, Any]:
    """Validate one settings layer (defaults or one provider)."""
    if layer is None:
        return {}
    if not isinstance(layer, dict):
        raise SettingsValidationError(
            f"{layer_name} must be an object", field=layer_name
        )
    cleaned: dict[str, Any] = {}
    for key, value in layer.items():
        if key not in _FIELDS:
            raise SettingsValidationError(
                f"unknown field: {key}", field=f"{layer_name}.{key}"
            )
        if key == "note" and layer_name == "defaults":
            # A global note would render the same text under every
            # provider row; ``note`` is per-provider only.
            raise SettingsValidationError(
                "note is only valid per provider", field=f"{layer_name}.{key}"
            )
        cleaned[key] = _normalize_field(key, value)
    return cleaned


def _normalize_notifications(value: Any) -> dict[str, Any]:
    """Validate the notifications opt-in block. Fail-closed on unknown keys.

    Each field defaults to its built-in value when missing — operators
    only state the fields they want to change. ``levels`` is normalised
    through a set so duplicates collapse and ordering is irrelevant;
    ``reminder_minutes`` is clamped into a sane range so the dialog never
    accidentally disables notifications forever or spams the operator.
    ``reminder_minutes`` defaults to zero so a brand-new alert notifies
    exactly once and never repeats until the operator sets an interval.
    """
    cleaned: dict[str, Any] = {}
    if value is None:
        return dict(_BUILTIN_NOTIFICATIONS)
    if not isinstance(value, dict):
        raise SettingsValidationError(
            "notifications must be an object", field="notifications"
        )
    allowed = {"enabled", "levels", "reminder_minutes"}
    extras = set(value) - allowed
    if extras:
        raise SettingsValidationError(
            f"unknown notifications fields: {sorted(extras)}",
            field="notifications",
        )
    # enabled: bool, defaults to False (opt-in).
    if "enabled" in value:
        raw = value["enabled"]
        if not isinstance(raw, bool):
            raise SettingsValidationError(
                "notifications.enabled must be a boolean",
                field="notifications.enabled",
            )
        cleaned["enabled"] = raw
    # levels: subset of {critical, low}, defaults to ["critical"].
    if "levels" in value:
        raw = value["levels"]
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise SettingsValidationError(
                "notifications.levels must be an array of strings",
                field="notifications.levels",
            )
        invalid = sorted({item for item in raw if item not in _NOTIFICATION_LEVELS})
        if invalid:
            raise SettingsValidationError(
                f"notifications.levels contains unknown values: {invalid}",
                field="notifications.levels",
            )
        # Preserve canonical order (critical first, then low) so the
        # on-disk payload is stable across reads/writes regardless of
        # the order the dialog sent them.
        ordered = [level for level in _NOTIFICATION_LEVELS_ORDER if level in raw]
        if not ordered:
            raise SettingsValidationError(
                "notifications.levels must contain at least one value",
                field="notifications.levels",
            )
        cleaned["levels"] = ordered
    # reminder_minutes: int in 0..1440. Zero switches repeats off; the
    # value is always validated so the on-disk payload stays well-formed.
    if "reminder_minutes" in value:
        raw = value["reminder_minutes"]
        # bool is a subclass of int; reject it explicitly so True/False
        # never slip through as 1/0.
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise SettingsValidationError(
                "notifications.reminder_minutes must be an integer in "
                f"{_NOTIFICATION_REMINDER_MIN}..{_NOTIFICATION_REMINDER_MAX}",
                field="notifications.reminder_minutes",
            )
        if raw < _NOTIFICATION_REMINDER_MIN or raw > _NOTIFICATION_REMINDER_MAX:
            raise SettingsValidationError(
                "notifications.reminder_minutes must be in "
                f"{_NOTIFICATION_REMINDER_MIN}..{_NOTIFICATION_REMINDER_MAX}",
                field="notifications.reminder_minutes",
            )
        cleaned["reminder_minutes"] = raw
    # Merge over the defaults so any field the operator omitted keeps its
    # built-in value. Done last so partial payloads round-trip cleanly.
    merged = dict(_BUILTIN_NOTIFICATIONS)
    merged.update(cleaned)
    return merged


def validate_payload(payload: Any) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
    """Validate a full settings payload and return the cleaned layers.

    Accepts ``{"defaults": {...}, "providers": {...}, "notifications": {...}}``.
    Unknown top-level keys are rejected, unknown fields inside ``defaults``/
    ``providers`` are rejected, and out-of-range threshold values raise
    :class:`SettingsValidationError`. The notifications block defaults are
    merged in when the operator omits them so partial PUTs round-trip
    cleanly.
    """
    if not isinstance(payload, dict):
        raise SettingsValidationError("payload must be an object")
    allowed_top = {"defaults", "providers", "notifications"}
    extras = set(payload) - allowed_top
    if extras:
        raise SettingsValidationError(f"unknown top-level keys: {sorted(extras)}")
    defaults = _normalize_layer(payload.get("defaults"), layer_name="defaults")
    raw_providers = payload.get("providers") or {}
    if not isinstance(raw_providers, dict):
        raise SettingsValidationError("providers must be an object")
    cleaned_providers: dict[str, dict[str, Any]] = {}
    for provider_id, layer in raw_providers.items():
        if not _valid_provider_id(provider_id):
            raise SettingsValidationError(
                f"invalid provider id: {provider_id!r}", field=provider_id
            )
        cleaned_providers[provider_id] = _normalize_layer(
            layer, layer_name=f"providers.{provider_id}"
        )
    notifications = _normalize_notifications(payload.get("notifications"))
    return defaults, cleaned_providers, notifications


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def _read_disk() -> dict[str, Any]:
    """Return the on-disk payload, or ``{}`` when the file is missing/empty.

    Corrupt JSON returns ``{}`` after a redacted warning: the file is
    operator-editable so the next PUT must overwrite it. The plugin never
    crashes because the settings file is malformed.
    """
    path = storage_path()
    if not path.is_file():
        return {"defaults": {}, "providers": {}, "notifications": {}}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        log.warning("settings file could not be read")
        return {"defaults": {}, "providers": {}, "notifications": {}}
    text = text.strip()
    if not text:
        return {"defaults": {}, "providers": {}, "notifications": {}}
    try:
        payload = json.loads(text)
    except ValueError:
        log.warning("settings file is not valid JSON; ignoring until next PUT")
        return {"defaults": {}, "providers": {}, "notifications": {}}
    if not isinstance(payload, dict):
        log.warning("settings file root is not an object; ignoring until next PUT")
        return {"defaults": {}, "providers": {}, "notifications": {}}
    return payload


def _safe_notifications(value: Any) -> dict[str, Any]:
    """Coerce a malformed notifications block into the built-in defaults.

    Read-side counterpart to :func:`_normalize_notifications`. The validator
    is the contract gate on write; the read path simply replaces any
    unreadable value with the safe defaults so the dashboard renders
    correctly without ever crashing on operator-edited JSON.
    """
    try:
        return _normalize_notifications(value)
    except SettingsValidationError:
        return dict(_BUILTIN_NOTIFICATIONS)


def load_raw() -> dict[str, Any]:
    """Return the raw disk payload (defaults + providers + notifications).

    Unknown keys are silently dropped on read so an old format never
    crashes the dashboard; PUT is the contract gate. Missing or
    malformed ``notifications`` blocks fall back to the built-in defaults
    so the UI can render without an explicit save round-trip.
    """
    raw = _read_disk()
    return {
        "defaults": _safe_layer(raw.get("defaults")),
        "providers": {
            str(provider_id): _safe_layer(layer)
            for provider_id, layer in (raw.get("providers") or {}).items()
            if _valid_provider_id(provider_id)
        },
        "notifications": _safe_notifications(raw.get("notifications")),
    }


def _safe_layer(layer: Any) -> dict[str, Any]:
    """Drop unknown fields from a disk layer; coerce malformed values to None."""
    if not isinstance(layer, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in layer.items():
        if key not in _FIELDS:
            continue
        try:
            cleaned[key] = _normalize_field(key, value)
        except SettingsValidationError:
            # A value the writer would reject must not bring the dashboard
            # down either: drop it and let the next PUT replace it.
            continue
    return cleaned


def save(payload: Any) -> dict[str, Any]:
    """Validate, persist, and return the cleaned payload.

    A failed validation raises :class:`SettingsValidationError` without
    touching the file. Successful writes go through a process-local lock and
    a write-temp-then-os.replace pattern so a partial file is never visible.
    """
    defaults, providers, notifications = validate_payload(payload)
    cleaned = {
        "defaults": defaults,
        "providers": providers,
        "notifications": notifications,
    }
    path = storage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(cleaned, indent=2, sort_keys=True) + "\n"
    with _WRITE_LOCK:
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        try:
            tmp.write_text(serialized, encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            log.warning("settings file could not be written")
            raise
    return cleaned


# ---------------------------------------------------------------------------
# Effective merge
# ---------------------------------------------------------------------------


def effective(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge ``defaults`` -> ``overrides`` -> built-in defaults.

    An override wins when it is not ``None``; an explicit ``False`` is a
    real value (e.g. ``balance_exhausted_at_zero=False``) and must NOT fall
    back to the default. ``None`` means "unset" everywhere — that is the
    signal that says "do not fire an alert".
    """
    merged: dict[str, Any] = {}
    for key in _FIELDS:
        if key in overrides and overrides[key] is not None:
            merged[key] = overrides[key]
        elif key in defaults and defaults[key] is not None:
            merged[key] = defaults[key]
        else:
            merged[key] = _BUILTIN_DEFAULTS[key]
    return merged


def effective_view(
    raw_defaults: dict[str, Any],
    raw_providers: dict[str, dict[str, Any]],
    *,
    provider_ids: Optional[tuple[str, ...]] = None,
) -> dict[str, dict[str, Any]]:
    """Return the merged effective settings for every provider.

    ``provider_ids`` lets the caller pin the dashboard order (and include
    catalog-only providers without settings). When omitted, only the keys
    that appear in ``raw_providers`` are returned; when provided, every
    id gets an effective row, falling back to ``raw_defaults`` for unset
    keys.
    """
    cleaned_defaults = _safe_layer(raw_defaults)
    effective_providers: dict[str, dict[str, Any]] = {}
    ids = list(raw_providers.keys())
    if provider_ids is not None:
        ids = list(dict.fromkeys(list(provider_ids) + ids))
    for provider_id in ids:
        if not _valid_provider_id(provider_id):
            continue
        layer = raw_providers.get(provider_id) or {}
        safe_layer = _safe_layer(layer)
        effective_providers[provider_id] = effective(cleaned_defaults, safe_layer)
    return effective_providers


def builtin_defaults() -> dict[str, Any]:
    """Return a copy of the built-in defaults."""
    return dict(_BUILTIN_DEFAULTS)


def builtin_notifications() -> dict[str, Any]:
    """Return a copy of the built-in notification opt-in defaults."""
    return {
        "enabled": _BUILTIN_NOTIFICATIONS["enabled"],
        "levels": list(_BUILTIN_NOTIFICATIONS["levels"]),
        "reminder_minutes": _BUILTIN_NOTIFICATIONS["reminder_minutes"],
    }


def known_fields() -> tuple[str, ...]:
    """Return the canonical field list, in declaration order."""
    return _FIELDS


def notification_levels() -> tuple[str, ...]:
    """Return the allowed notification alert levels, in canonical order."""
    return _NOTIFICATION_LEVELS_ORDER


def notification_reminder_minutes() -> tuple[int, int]:
    """Return the operator-controllable ``reminder_minutes`` range.

    The UI uses this range so the dialog input mirrors the validator
    without hardcoding constants in two places. The closed interval
    ``[0, 1440]`` is the spec range — zero means "no repeats" — and
    anything outside it fails closed at the validator.
    """
    return (_NOTIFICATION_REMINDER_MIN, _NOTIFICATION_REMINDER_MAX)


def note_max_length() -> int:
    """Return the maximum length for the ``note`` field."""
    return _NOTE_MAX_LEN
