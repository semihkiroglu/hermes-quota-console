"""Compatibility adapter for the Hermes runtime.

This is the single boundary where the plugin touches Hermes internals:
credential resolution, plugin configuration, auth-store locking, profile
discovery and path resolution, pool cooldown math, account usage, and the
documented Codex usage cache. Provider adapters and the plugin API depend only
on this module's stable functions, never on ``hermes_cli`` or ``agent`` imports.

Every Hermes import is lazy and every function fails closed: a missing or
incompatible Hermes API returns a safe default (or raises
:class:`RuntimeUnavailable`) and logs a redacted feature name. Exception
text, paths, tokens, and provider payloads are never logged.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("hermes-quota-console.runtime")

_CODEX_CACHE_NAME = "codex_usage_state.json"
_CODEX_CACHE_EMPTY = "{}\n"


class RuntimeUnavailable(RuntimeError):
    """A required Hermes runtime API is missing or incompatible."""


def _log_unavailable(feature: str) -> None:
    # Deliberately redacted: feature names only, no exception text or paths.
    log.warning("usage runtime feature unavailable: %s", feature)


def hermes_default_root() -> Optional[Path]:
    """Resolve the Hermes root directory through the public path helper.

    Uses ``hermes_constants.get_default_hermes_root`` (the configured Hermes
    home) instead of any fixed installation path. Returns ``None`` when the
    helper is unavailable.
    """
    try:
        from hermes_constants import get_default_hermes_root

        return get_default_hermes_root()
    except Exception:
        _log_unavailable("hermes root path")
        return None


def resolve_provider_credentials(provider: str) -> Optional[dict[str, Any]]:
    """Resolve one provider through Hermes' runtime credential machinery."""
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested=provider)
    except Exception:
        _log_unavailable("runtime provider")
        return None
    if not isinstance(runtime, dict):
        return None
    token = str(runtime.get("api_key") or "").strip()
    return runtime if token else None


def codex_configured() -> bool:
    """Return whether Codex is selected or has a credential pool entry."""
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
        model = config.get("model") if isinstance(config, dict) else None
        if (
            isinstance(model, dict)
            and str(model.get("provider") or "").strip().lower() == "openai-codex"
        ):
            return True
    except Exception:
        pass
    try:
        from agent.credential_pool import load_pool

        return bool(load_pool("openai-codex").entries())
    except Exception:
        return False


def load_plugin_provider_allowlist() -> Optional[tuple[str, ...]]:
    """Return the explicit ``plugins.quota-console.providers`` configuration.

    ``None`` means no valid allowlist is configured, which keeps the registry
    on its built-in defaults. An explicit empty list returns an empty tuple so
    operators can disable every provider. Hermes imports stay inside this
    compatibility boundary and failures remain fail-closed.
    """
    try:
        from hermes_cli.config import load_config  # type: ignore[import-not-found]

        config = load_config() or {}
    except Exception:
        return None
    if not isinstance(config, dict):
        return None
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        return None
    usages = plugins.get("quota-console")
    if not isinstance(usages, dict):
        return None
    raw = usages.get("providers")
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return None
    provider_ids = {
        item.strip().lower()
        for item in raw
        if isinstance(item, str) and item.strip()
    }
    return tuple(sorted(provider_ids))


def fetch_account_usage(provider: str) -> Optional[Any]:
    """Fetch Hermes' normalized account usage snapshot for one provider."""
    try:
        from agent.account_usage import fetch_account_usage as _fetch

        return _fetch(provider)
    except Exception:
        _log_unavailable("account usage")
        return None


def list_profiles() -> list[dict[str, Any]]:
    """Return safe default-model rows for every live Hermes profile.

    Profile paths come from Hermes' own discovery; no path is built here
    from a fixed installation location.
    """
    try:
        from hermes_cli import profiles as profiles_mod

        infos = profiles_mod.list_profiles()
    except Exception:
        _log_unavailable("profile list")
        return []

    rows: list[dict[str, Any]] = []
    for info in infos:
        name = str(getattr(info, "name", "") or "").strip().lower()
        path = getattr(info, "path", None)
        if not name or not isinstance(path, Path):
            continue
        model = getattr(info, "model", None)
        provider = getattr(info, "provider", None)
        rows.append(
            {
                "name": name,
                "path": path,
                "model": str(model).strip()[:200] if model is not None else None,
                "provider": (
                    str(provider).strip().lower()[:100] if provider is not None else None
                ),
            }
        )
    return rows


def list_catalog_providers() -> list[dict[str, Any]]:
    """Return Hermes' current provider catalog as safe rows.

    The catalog powers the profile-only layer: every provider Hermes can
    use shows up in the dashboard even when this plugin has no quota
    adapter for it yet. Only ``slug``, ``label`` and the ``keyless`` flag
    cross the boundary; auth env var names are deliberately omitted so no
    credential source is advertised to the browser. ``keyless`` matters
    because such providers are usable without any stored credential and
    must count as configured.
    """
    try:
        import hermes_cli
        from hermes_cli.provider_catalog import provider_catalog
    except Exception:
        _log_unavailable("provider catalog")
        return []
    _pin_hermes_providers_package()
    try:
        entries = provider_catalog()
    except Exception:
        _log_unavailable("provider catalog")
        return []
    rows: list[dict[str, Any]] = []
    for entry in entries:
        slug = str(getattr(entry, "slug", "") or "").strip().lower()
        label = str(getattr(entry, "label", "") or "").strip()
        if not slug:
            continue
        rows.append(
            {
                "slug": slug,
                "label": label[:80] if label else slug,
                "keyless": bool(getattr(entry, "keyless", False)),
            }
        )
    return rows


def _pin_hermes_providers_package() -> None:
    """Ensure Hermes' own ``providers`` package wins the bare import.

    ``hermes_cli.provider_catalog`` resolves ``from providers import
    list_providers`` against ``sys.path``. This plugin ships a
    ``dashboard/providers`` package that can shadow Hermes' package when
    the plugin directory sits earlier on the path, silently dropping the
    provider-plugin entries (e.g. OpenCode Free) from the catalog. Load
    Hermes' package explicitly into ``sys.modules`` so the bare import
    finds it there without touching ``sys.path`` order.
    """
    try:
        import importlib.util
        import sys
        from pathlib import Path

        import hermes_cli as _hermes_cli

        if "providers" in sys.modules:
            return
        hermes_root = Path(_hermes_cli.__file__).resolve().parents[1]
        package_dir = hermes_root / "providers"
        if not package_dir.is_dir() or not (package_dir / "__init__.py").is_file():
            return
        spec = importlib.util.spec_from_file_location(
            "providers",
            package_dir / "__init__.py",
            submodule_search_locations=[str(package_dir)],
        )
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules["providers"] = module
        spec.loader.exec_module(module)
    except Exception:
        # Keep the catalog usable: the plugin's own providers package may
        # shadow Hermes', but a failed pin must not break discovery.
        return


def normalize_profile_name(name: str) -> str:
    """Normalize a profile name through Hermes' canonical naming rules."""
    try:
        from hermes_cli import profiles as profiles_mod

        return profiles_mod.normalize_profile_name(name)
    except Exception:
        _log_unavailable("profile names")
        raise RuntimeUnavailable("profile names unavailable") from None


def auth_store_path(profile_path: Path) -> Path:
    """Derive one profile's auth store location from its profile path."""
    return Path(profile_path) / "auth.json"


def read_auth_store(profile_path: Path) -> Optional[dict[str, Any]]:
    """Read one profile auth store under Hermes' cross-process lock.

    The core auth module owns migration, corruption handling, atomic writes,
    and permissions. The adapter only supplies an explicit profile path and
    never prints the returned credential payload.
    """
    path = auth_store_path(profile_path)
    try:
        from hermes_cli import auth as auth_mod

        with auth_mod._auth_store_lock(target_path=path):
            store = auth_mod._load_auth_store(path)
        return store if isinstance(store, dict) else None
    except Exception:
        _log_unavailable("auth store")
        return None


def update_auth_store(
    profile_path: Path, mutator: Callable[[dict[str, Any]], int]
) -> int:
    """Lock, load, mutate, and persist one profile auth store atomically.

    ``mutator`` receives the loaded store dict and returns the number of
    credential-pool entries it changed. The store is persisted only when the
    mutator reports changes, so untouched stores stay byte-for-byte intact
    on disk.
    """
    path = auth_store_path(profile_path)
    try:
        from hermes_cli import auth as auth_mod
    except Exception:
        _log_unavailable("auth store")
        raise RuntimeUnavailable("auth store unavailable") from None
    try:
        with auth_mod._auth_store_lock(target_path=path):
            store = auth_mod._load_auth_store(path)
            if not isinstance(store, dict):
                store = {"version": 1, "providers": {}}
            changed = mutator(store)
            if changed:
                auth_mod._save_auth_store(store, target_path=path)
            return changed
    except RuntimeUnavailable:
        raise
    except Exception:
        _log_unavailable("auth store write")
        raise RuntimeUnavailable("auth store unavailable") from None


def exhausted_until(
    provider: str, entry: dict[str, Any], *, sole_credential: bool = False
) -> Optional[float]:
    """Reuse Hermes' TTL calculation for an exhausted pool entry."""
    try:
        from agent.credential_pool import PooledCredential, _exhausted_until

        pooled = PooledCredential.from_dict(provider, entry)
        return _exhausted_until(pooled, sole_credential=sole_credential)
    except Exception:
        return None


def clear_codex_usage_cache() -> bool:
    """Atomically clear the documented shared Codex usage cache.

    The cache lives at ``<Hermes root>/codex_usage_state.json`` and is
    resolved through the public Hermes path helper. Returns ``False`` when
    the cache file does not exist; raises :class:`RuntimeUnavailable` when
    the Hermes root cannot be resolved or the cache cannot be cleared.
    """
    root = hermes_default_root()
    if root is None:
        raise RuntimeUnavailable("codex usage cache unavailable") from None
    path = root / _CODEX_CACHE_NAME
    if not path.is_file():
        return False
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        temporary.write_text(_CODEX_CACHE_EMPTY, encoding="utf-8")
        os.replace(temporary, path)
        return True
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        _log_unavailable("codex usage cache")
        raise RuntimeUnavailable("codex usage cache unavailable") from None
