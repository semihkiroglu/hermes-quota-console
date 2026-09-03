"""Operator-editable settings for the quota-console dashboard plugin.

Settings persist in a per-plugin JSON file outside the live install tree, so
``hermes plugins update`` and ``hermes plugins remove`` never touch operator
state. The storage location follows the hermes-achievements bundle precedent::

    <hermes_home>/plugin-data/quota-console/config.json

When Hermes' ``plugins.plugin_storage`` helper is importable we use
``plugin_data_dir("quota-console")`` directly; otherwise we fall back to the
same layout via ``hermes_constants.get_hermes_home()`` so the plugin keeps
working in standalone test environments.

The on-disk shape is two-layer::

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
      }
    }

Effective value = provider override if set, else global default, else the
built-in default (which is ``None`` for every threshold). The module never
stores credential values, endpoint URLs, or mapping paths — only the
alert-threshold fields and the per-provider note.

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


def validate_payload(payload: Any) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Validate a full settings payload and return the cleaned layers.

    Accepts ``{"defaults": {...}, "providers": {...}}``. Unknown top-level
    keys are rejected, unknown fields inside ``defaults``/``providers`` are
    rejected, and out-of-range threshold values raise :class:`SettingsValidationError`.
    """
    if not isinstance(payload, dict):
        raise SettingsValidationError("payload must be an object")
    allowed_top = {"defaults", "providers"}
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
    return defaults, cleaned_providers


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
        return {"defaults": {}, "providers": {}}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        log.warning("settings file could not be read")
        return {"defaults": {}, "providers": {}}
    text = text.strip()
    if not text:
        return {"defaults": {}, "providers": {}}
    try:
        payload = json.loads(text)
    except ValueError:
        log.warning("settings file is not valid JSON; ignoring until next PUT")
        return {"defaults": {}, "providers": {}}
    if not isinstance(payload, dict):
        log.warning("settings file root is not an object; ignoring until next PUT")
        return {"defaults": {}, "providers": {}}
    return payload


def load_raw() -> dict[str, Any]:
    """Return the raw disk payload (defaults + providers), unmerged.

    Unknown keys are silently dropped on read so an old format never
    crashes the dashboard; PUT is the contract gate.
    """
    raw = _read_disk()
    return {
        "defaults": _safe_layer(raw.get("defaults")),
        "providers": {
            str(provider_id): _safe_layer(layer)
            for provider_id, layer in (raw.get("providers") or {}).items()
            if _valid_provider_id(provider_id)
        },
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
    defaults, providers = validate_payload(payload)
    cleaned = {"defaults": defaults, "providers": providers}
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


def known_fields() -> tuple[str, ...]:
    """Return the canonical field list, in declaration order."""
    return _FIELDS


def note_max_length() -> int:
    """Return the maximum length for the ``note`` field."""
    return _NOTE_MAX_LEN
