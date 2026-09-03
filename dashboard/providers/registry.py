"""Loadable provider registry for the quota-console dashboard plugin.

The registry is the sole assembly point for provider adapters. Built-in
adapters ship with the plugin and remain available by default. Installed
packages may contribute additional adapters through the Python entry-point
group ``hermes_quota_console.providers``, but an explicit Hermes plugin
configuration allowlist (``plugins.quota-console.providers``) is required
before any external adapter is loaded. Without an allowlist the registry
returns only the built-in adapters, so provider scope is never inferred
from environment variables or credential presence.
"""

from __future__ import annotations

import importlib
from importlib import metadata
import logging
import re
from typing import Any, Optional

from .base import ProviderSpec
from .deepseek import SPEC as DEEPSEEK
from .minimax import SPEC as MINIMAX
from .openai_codex import SPEC as OPENAI_CODEX

log = logging.getLogger(__name__)

# Hermes may load plugin_api.py under a dotted standalone name whose outer
# package does not exist. Mirror the API loader's explicit module-name strategy
# instead of relying on a relative import through that absent parent package.
_runtime = importlib.import_module(
    f"{__name__.rsplit('.providers.', 1)[0]}.runtime"
)

_ENTRY_POINT_GROUP = "hermes_quota_console.providers"
_ENTRY_POINT_ATTR = "SPEC"

# Provider IDs are stable lowercase identifiers; anything else is rejected.
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
# Entry-point module paths come from installed package metadata, but the
# import destination is still validated so nothing arbitrary can be loaded.
_MODULE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def _valid_id(provider_id: Any) -> bool:
    return isinstance(provider_id, str) and bool(_ID_PATTERN.fullmatch(provider_id))


# Keep this order aligned with the dashboard's compact card layout.
_BUILTIN_SPECS: tuple[ProviderSpec, ...] = (DEEPSEEK, OPENAI_CODEX, MINIMAX)


def _profile_only(provider_id: str, label: str, *, keyless: bool = False) -> ProviderSpec:
    """Build a profile-only spec for a catalog provider with no quota adapter.

    The provider shows up in the dashboard (identity/profile layer) but
    never performs a quota fetch; its card simply has no snapshot, so the
    UI renders the availability row and assigned profiles only. This is
    the two-layer contract: identity for every Hermes provider, quota only
    where a verified source exists. ``keyless`` marks providers that are
    usable without stored credentials (e.g. OpenCode Free).
    """

    def fetch(context: Any) -> None:
        return None

    return ProviderSpec(
        id=provider_id,
        label=label,
        fetch=fetch,
        has_quota=False,
        keyless=keyless,
    )


def _catalog_extension_specs() -> tuple[ProviderSpec, ...]:
    """Merge Hermes' current provider catalog into the registry.

    Built-in adapters keep their real quota fetch; every other catalog
    provider becomes a profile-only spec in catalog order. The catalog is
    optional and fail-closed: when Hermes is unavailable the registry
    simply keeps its built-ins.
    """
    builtin_by_id = {spec.id: spec for spec in _BUILTIN_SPECS}
    rows = _runtime.list_catalog_providers()
    merged: dict[str, ProviderSpec] = {}
    for row in rows:
        provider_id = str(row.get("slug") or "").strip().lower()
        if not _valid_id(provider_id):
            continue
        if provider_id in builtin_by_id:
            # Built-in quota adapters win over catalog identity rows.
            merged[provider_id] = builtin_by_id[provider_id]
        else:
            label = str(row.get("label") or provider_id).strip()
            merged[provider_id] = _profile_only(
                provider_id,
                label,
                keyless=bool(row.get("keyless", False)),
            )
    # Catalog order decides the dashboard order; built-ins that are not
    # part of the catalog (defensive) keep their registry position at the
    # end of the merged tuple.
    specs = [merged[provider_id] for provider_id in merged]
    for spec in _BUILTIN_SPECS:
        if spec.id not in merged:
            specs.append(spec)
    return tuple(specs)


def _load_allowlist() -> Optional[tuple[str, ...]]:
    """Read the explicit provider allowlist from Hermes plugin config.

    Returns ``None`` when no allowlist is configured, which keeps the built-in
    adapters as the only scope. An empty tuple is an explicit operator choice
    that disables every registered provider.
    """
    return _runtime.load_plugin_provider_allowlist()


def _discover_entry_points() -> list[metadata.EntryPoint]:
    """Return sorted, deduplicated entry points from the provider group.

    Discovery never raises: a broken metadata backend fails closed to no
    external adapters. Malformed IDs are dropped before ordering so the result
    is deterministic.
    """
    try:
        discovered = list(metadata.entry_points(group=_ENTRY_POINT_GROUP))
    except Exception:
        log.warning("provider entry-point discovery failed")
        return []
    valid = [
        entry
        for entry in discovered
        if isinstance(entry, metadata.EntryPoint) and _valid_id(entry.name)
    ]
    valid.sort(key=lambda entry: entry.name)
    deduplicated: dict[str, metadata.EntryPoint] = {}
    for entry in valid:
        # First occurrence in sorted order wins; later duplicates are ignored.
        deduplicated.setdefault(entry.name, entry)
    return list(deduplicated.values())


def _load_external_spec(entry_point: metadata.EntryPoint) -> Optional[ProviderSpec]:
    """Load one external adapter, failing closed to ``None`` on any problem.

    Only the stable provider ID is logged on failure; module paths, exception
    details, and attribute names are deliberately omitted.
    """
    module_name, separator, attr = entry_point.value.partition(":")
    if not separator or not module_name:
        module_name, attr = entry_point.value, _ENTRY_POINT_ATTR
    if not _MODULE_PATTERN.fullmatch(module_name) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", attr or ""
    ):
        log.warning("provider entry point skipped")
        return None
    try:
        module = importlib.import_module(module_name)
        spec = getattr(module, attr)
    except Exception:
        log.warning("provider entry point skipped")
        return None
    if not isinstance(spec, ProviderSpec) or spec.id != entry_point.name:
        log.warning("provider entry point skipped")
        return None
    return spec


def _build_registry(allowlist: Optional[tuple[str, ...]]) -> tuple[ProviderSpec, ...]:
    """Assemble the final provider tuple from the explicit allowlist."""
    if allowlist is None:
        # No explicit operator scope: built-in adapters plus every provider
        # in Hermes' catalog as profile-only identity rows. External entry
        # points still require an explicit allowlist before they are loaded.
        return _catalog_extension_specs()

    allowed = {str(item).strip().lower() for item in allowlist}
    specs = [spec for spec in _BUILTIN_SPECS if spec.id in allowed]
    seen = {spec.id for spec in specs}
    for entry_point in _discover_entry_points():
        if entry_point.name in seen:
            # A built-in or an earlier entry point already owns this ID.
            continue
        if entry_point.name not in allowed:
            continue
        spec = _load_external_spec(entry_point)
        if spec is None:
            continue
        specs.append(spec)
        seen.add(spec.id)
    return tuple(specs)


def load() -> tuple[ProviderSpec, ...]:
    """Build the registry from the explicit Hermes plugin allowlist.

    This function intentionally performs no caching: callers cache the
    returned tuple on the time scale they want (the plugin API uses a
    short-lived cache so a config change shows up without a dashboard
    restart). Tests can monkeypatch ``_load_allowlist`` to flip the
    allowlist between calls and observe the new scope immediately.
    """
    return _build_registry(_load_allowlist())


# ``PROVIDERS`` is kept as a snapshot of the built-in defaults for tests
# and tooling that need a stable reference without performing live config
# reads. The plugin API does not rely on this constant; it calls
# ``registry.load()`` on each summary/reset so an updated allowlist
# reaches the dashboard without a process restart.
PROVIDERS = load()
