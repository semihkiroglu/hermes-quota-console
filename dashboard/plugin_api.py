"""Quota and rate-limit control API for the Hermes dashboard quota-console
plugin.

The router is mounted at ``/api/plugins/quota-console/`` by Hermes.
Provider-specific logic lives in ``providers/``; this module owns the
shared contract, the fixed-host HTTP client, profile model status cards,
and safe token-preserving rate-limit reset actions.

Every Hermes runtime dependency (credential resolution, auth-store locking,
profile discovery and path resolution, pool cooldown math, account usage,
and the Codex usage cache) crosses the compatibility boundary in
``runtime.py``. This module never imports Hermes packages directly, so a
missing or incompatible Hermes runtime fails closed with generic errors and
redacted logs.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import importlib
import logging
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Literal, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

# Hermes loads plugin_api.py as a standalone module via spec_from_file_location.
# Mark that module as a package so provider adapters and the runtime adapter
# can use stable relative imports without colliding with Hermes' own
# top-level ``providers`` package.
_DASHBOARD_DIR = Path(__file__).resolve().parent
# Load ``dashboard.settings`` via spec_from_file_location so settings resolves
# regardless of whether ``__name__`` is ``dashboard.plugin_api`` (Hermes) or a
# synthetic test module name (pytest fixtures load plugin_api without
# ``dashboard`` on sys.path). The dashboard directory has no ``__init__.py``,
# so a normal ``import dashboard.settings`` would fail.
import importlib.util as _importlib_util  # noqa: E402

_settings_spec = _importlib_util.spec_from_file_location(
    "dashboard.settings", _DASHBOARD_DIR / "settings.py"
)
if _settings_spec is None or _settings_spec.loader is None:
    raise ImportError("dashboard.settings could not be loaded")
_settings = _importlib_util.module_from_spec(_settings_spec)
sys.modules.setdefault("dashboard.settings", _settings)
_settings_spec.loader.exec_module(_settings)
SettingsValidationError = _settings.SettingsValidationError

_current_module = sys.modules.get(__name__)
if _current_module is not None:
    _current_module.__path__ = [str(_DASHBOARD_DIR)]

_base = importlib.import_module(f"{__name__}.providers.base")
_registry = importlib.import_module(f"{__name__}.providers.registry")
_runtime = importlib.import_module(f"{__name__}.runtime")
ProviderContext = _base.ProviderContext
# ``PROVIDERS`` is the built-in default snapshot, kept for tests and
# tooling. The summary and reset paths call ``registry.load()`` per
# request so an updated allowlist reaches the dashboard without a
# process restart; see ``_current_providers``.
PROVIDERS = _registry.PROVIDERS
base_card = _base.base_card
unavailable = _base.unavailable
annotate_items = _base.annotate_items
bucket_alert = _base.bucket_alert
runtime = _runtime
RuntimeUnavailable = _runtime.RuntimeUnavailable

router = APIRouter()
log = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 30.0
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_CACHE_LOCK = threading.Lock()
_CACHE_BUILD_LOCK = threading.Lock()
_CACHE: Optional[dict[str, Any]] = None
_CACHE_AT = 0.0


def _plugin_version() -> Optional[str]:
    """Return the plugin version from pyproject.toml, or ``None``.

    The version is read from the project file next to the dashboard
    directory so the footer can show the same version the release
    workflow tags. A missing or malformed file fails closed to ``None``
    (the UI simply hides the version chip); the file is tiny and read
    on every summary build, so no caching is needed.
    """
    try:
        pyproject = _DASHBOARD_DIR.parent / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("version"):
            # TOML string literal: version = "1.2.3"
            marker = stripped.split("=", 1)
            if len(marker) == 2:
                raw = marker[1].strip().strip('"').strip("'")
                if raw:
                    return raw
    return None


# Latest-release check for the footer. Web browsers cannot call the
# GitHub releases API directly without exposing the dashboard session
# token through the plugin's authenticated fetch, so the check runs
# server-side against a public, no-auth endpoint and caches the answer
# for 15 minutes (the GitHub anonymous rate limit is per-IP and small;
# 15 min keeps the footer fresh while staying far below the budget).
_LATEST_RELEASE_API = "https://api.github.com/repos/semihkiroglu/hermes-quota-console/releases/latest"
_LATEST_RELEASE_TTL_SECONDS = 900.0
_LATEST_RELEASE_CACHE: dict[str, Any] = {"at": 0.0, "value": None}


def _latest_release() -> Optional[str]:
    """Return the newest published release tag (e.g. ``v0.1.3``) or ``None``.

    Fail-closed: network errors, rate limits, or a missing release all
    yield ``None`` so the footer simply shows nothing when the check is
    unavailable. The result is cached for an hour to stay inside the
    anonymous GitHub rate budget.
    """
    now = time.monotonic()
    cache = _LATEST_RELEASE_CACHE.get("value")
    if cache is not None and now - float(_LATEST_RELEASE_CACHE.get("at", 0.0)) < _LATEST_RELEASE_TTL_SECONDS:
        return cache
    try:
        with httpx.Client(timeout=5.0, follow_redirects=False) as client:
            response = client.get(_LATEST_RELEASE_API, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
        tag = str(payload.get("tag_name") or "") if isinstance(payload, dict) else ""
    except (httpx.HTTPError, ValueError, TypeError):
        return None
    with _CACHE_LOCK:
        if tag:
            _LATEST_RELEASE_CACHE["at"] = now
            _LATEST_RELEASE_CACHE["value"] = tag
    return tag or None


class UsageProviderError(RuntimeError):
    """Expected provider/auth/response failure without sensitive details."""


class RateLimitResetError(RuntimeError):
    """Expected failure while changing a profile's cached auth state."""


def _get_json(
    url: str,
    token: str,
    *,
    headers: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """GET a small JSON document without following redirects."""
    request_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "hermes-quota-console/0.1",
    }
    if headers:
        request_headers.update(headers)
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=False) as client:
            response = client.get(url, headers=request_headers)
            response.raise_for_status()
            if len(response.content) > 1_000_000:
                raise UsageProviderError("provider response too large")
            payload = response.json()
    except UsageProviderError:
        raise
    except (httpx.HTTPError, ValueError, TypeError):
        # Do not include response text or exception details: providers may put
        # account identifiers or request URLs in them. The chained cause is
        # dropped so nothing sensitive survives in tracebacks either.
        raise UsageProviderError("provider request failed") from None
    if not isinstance(payload, dict):
        raise UsageProviderError("provider response was not an object")
    return payload


def _log_unavailable(provider_id: str) -> None:
    # Deliberately omit exception/URL/token details from the dashboard log.
    log.warning("usage provider %s unavailable", provider_id)


def _build_context() -> ProviderContext:
    """Bind the provider contract to the current runtime adapter functions.

    The context is rebuilt per summary build so a swapped or patched runtime
    adapter (as the tests do) takes effect without a process restart.
    """
    return ProviderContext(
        runtime_credentials=runtime.resolve_provider_credentials,
        get_json=_get_json,
        base_card=base_card,
        unavailable=unavailable,
        codex_configured=runtime.codex_configured,
        fetch_account_usage=runtime.fetch_account_usage,
        log_unavailable=_log_unavailable,
    )


# Kept for backwards compatibility with code that imports the module-level
# context; summary builds use a fresh binding via _build_context().
_CONTEXT = _build_context()


# ``_RESET_PROVIDERS`` and ``_current_providers`` resolve at request time
# rather than at import time so a Hermes plugin-config allowlist change
# (or an entry-point installation) reaches the dashboard without a
# process restart. Both paths still go through ``registry.load()`` which
# honours the explicit allowlist and fail-closed behaviour.
def _current_providers() -> tuple[Any, ...]:
    """Return the live provider tuple for the current allowlist."""
    return _registry.load()


def _reset_providers() -> tuple[str, ...]:
    """Return the provider IDs reset touches on every profile.

    Only adapters with a real quota source are reset; catalog-only
    (profile-only) providers have no fetched state to clear.
    """
    return tuple(
        spec.id for spec in _current_providers() if getattr(spec, "has_quota", True)
    )


# Model cards intentionally follow the profile's selected default model. The
# profile may contain auxiliary/delegation settings, but those are not the
# model the operator asked to inspect here. Resetting a profile clears only
# that profile's own pool entries for every supported provider: all
# configured models share provider-level rate-limit state, and reset never
# copies credentials between profiles.
_RESET_FIELDS = (
    "last_status",
    "last_status_at",
    "last_error_code",
    "last_error_reason",
    "last_error_message",
    "last_error_reset_at",
)
_STATUS_LABELS = {
    "ready": "Ready",
    "rate_limited": "Rate limited",
    "auth_failed": "Auth failed",
    "degraded": "Degraded",
    "unconfigured": "Not configured",
    "unavailable": "Unavailable",
    "profile_only": "No quota source",
    "untracked": "Not tracked",
}


def _parse_timestamp(value: Any) -> Optional[float]:
    """Parse persisted epoch/ISO timestamps without exposing raw values."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        return number / 1000.0 if number > 1_000_000_000_000 else number
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            number = float(raw)
        except ValueError:
            number = None
        if number is not None:
            return number / 1000.0 if number > 1_000_000_000_000 else number
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _pool_entries(store: Optional[dict[str, Any]], provider: str) -> list[dict[str, Any]]:
    if not isinstance(store, dict):
        return []
    pool = store.get("credential_pool")
    if not isinstance(pool, dict):
        return []
    entries = pool.get(provider)
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _provider_state_has_auth(store: Optional[dict[str, Any]], provider: str) -> bool:
    """Detect singleton auth blocks without returning any credential value."""
    if not isinstance(store, dict):
        return False
    providers = store.get("providers")
    state = providers.get(provider) if isinstance(providers, dict) else None
    if not isinstance(state, dict):
        return False
    for key in ("api_key", "access_token", "refresh_token", "token"):
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return bool(state.get("logged_in"))


def _profile_rows() -> list[dict[str, Any]]:
    """Return safe default-model rows for every live Hermes profile."""
    try:
        return runtime.list_profiles()
    except RuntimeUnavailable:
        log.warning("usage profile list unavailable")
        return []


def _entry_reset_at(provider: str, entry: dict[str, Any], sole_credential: bool) -> Optional[float]:
    explicit = _parse_timestamp(entry.get("last_error_reset_at"))
    if explicit is not None:
        return explicit
    if entry.get("last_status") != "exhausted":
        return None
    # Reuse Hermes' TTL calculation so the card agrees with pool selection.
    return runtime.exhausted_until(provider, entry, sole_credential=sole_credential)


def _model_status(
    provider: Optional[str],
    entries: list[dict[str, Any]],
    effective_store: Optional[dict[str, Any]],
) -> tuple[str, dict[str, int], Optional[float]]:
    if not provider or provider not in _reset_providers():
        return "untracked", {"total": 0, "available": 0, "rate_limited": 0, "auth_failed": 0}, None

    if not entries:
        if _provider_state_has_auth(effective_store, provider):
            return "ready", {"total": 1, "available": 1, "rate_limited": 0, "auth_failed": 0}, None
        return "unconfigured", {"total": 0, "available": 0, "rate_limited": 0, "auth_failed": 0}, None

    rate_limited = 0
    auth_failed = 0
    available = 0
    reset_times: list[float] = []
    non_dead = 0
    for entry in entries:
        if str(entry.get("last_status") or "").strip().lower() == "dead":
            auth_failed += 1
            continue
        non_dead += 1
        if str(entry.get("last_status") or "").strip().lower() == "exhausted":
            rate_limited += 1
        else:
            available += 1

    sole_credential = non_dead <= 1
    for entry in entries:
        if str(entry.get("last_status") or "").strip().lower() == "exhausted":
            reset_at = _entry_reset_at(provider, entry, sole_credential)
            if reset_at is not None:
                reset_times.append(reset_at)

    total = len(entries)
    counts = {
        "total": total,
        "available": available,
        "rate_limited": rate_limited,
        "auth_failed": auth_failed,
    }
    if available:
        status = "degraded" if rate_limited or auth_failed else "ready"
    elif rate_limited:
        status = "rate_limited"
    elif auth_failed:
        status = "auth_failed"
    else:
        status = "ready"
    return status, counts, min(reset_times) if reset_times else None


def _build_model_cards() -> list[dict[str, Any]]:
    rows = _profile_rows()
    if not rows:
        return []
    default_row = next((row for row in rows if row["name"] == "default"), rows[0])
    root_store = runtime.read_auth_store(default_row["path"])
    cards: list[dict[str, Any]] = []
    for row in rows:
        profile_store = runtime.read_auth_store(row["path"])
        provider = row["provider"]
        local_entries = _pool_entries(profile_store, provider) if provider else []
        if local_entries:
            entries = local_entries
            effective_store = profile_store
        else:
            entries = _pool_entries(root_store, provider) if provider else []
            effective_store = root_store
        status, counts, reset_at = _model_status(provider, entries, effective_store)
        cards.append({
            "id": row["name"],
            "profile": row["name"],
            "model": row["model"],
            "provider": provider,
            "status": status,
            "status_label": _STATUS_LABELS[status],
            "credentials": counts,
            "reset_at": datetime.fromtimestamp(reset_at, timezone.utc).isoformat() if reset_at else None,
        })
    return cards


def _clear_entry_status(entry: dict[str, Any], reset_timestamp: float) -> bool:
    """Clear cooldown/error metadata and beat disk-merge timestamps."""
    changed = False
    for field in _RESET_FIELDS:
        if field in entry and entry.get(field) is not None:
            entry[field] = None
            changed = True
    if "failure_reason" in entry:
        entry.pop("failure_reason", None)
        changed = True
    extra = entry.get("extra")
    if isinstance(extra, dict) and "failure_reason" in extra:
        extra.pop("failure_reason", None)
        changed = True
    if changed:
        # write_credential_pool deliberately preserves a newer on-disk
        # cooldown. A reset timestamp in the present makes this explicit
        # reset newer than the stale disk record, so the cooldown cannot be
        # resurrected during persistence.
        entry["last_status_at"] = reset_timestamp
    return changed


def _reset_store_provider(profile_path: Path, provider: str) -> int:
    """Clear status metadata in one profile store, preserving credentials.

    The mutator only touches exhaustion/rate-limit metadata fields. Credential
    values (``access_token``, ``refresh_token``, ``api_key``, ``token``) are
    never read back into this module and never modified.
    """

    def mutator(store: dict[str, Any]) -> int:
        pool = store.get("credential_pool")
        if not isinstance(pool, dict):
            pool = {}
            store["credential_pool"] = pool
        entries = _pool_entries(store, provider)
        reset_timestamp = time.time()
        return sum(
            1 for entry in entries if _clear_entry_status(entry, reset_timestamp)
        )

    try:
        return runtime.update_auth_store(profile_path, mutator)
    except RuntimeUnavailable:
        raise RateLimitResetError("profile auth state could not be updated") from None


def _reset_profile_rows(rows: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """Reset every supported provider pool inside the given profile rows."""
    count = 0
    errors: list[str] = []
    for row in rows:
        for provider in _reset_providers():
            try:
                count += _reset_store_provider(row["path"], provider)
            except RateLimitResetError:
                errors.append(provider)
    return count, errors


def _reset_profiles(scope: Literal["profile", "all", "provider"], profile_name: Optional[str], provider_id: Optional[str] = None) -> dict[str, Any]:
    rows = _profile_rows()
    if not rows:
        raise RateLimitResetError("no live profiles found")
    by_name = {row["name"]: row for row in rows}
    results: list[dict[str, Any]] = []

    if scope == "profile":
        target = by_name.get(profile_name or "")
        if target is None:
            raise RateLimitResetError("profile does not exist")
        count, errors = _reset_profile_rows([target])
        results.append({"profile": target["name"], "reset_credentials": count, "errors": errors})
    elif scope == "provider":
        # Reset one provider's pool entries across every live profile. This
        # is the availability-row action: the provider row clears rate-limit
        # state for all profiles using that provider, not just one.
        provider = (provider_id or "").strip().lower()
        if provider not in _reset_providers():
            raise RateLimitResetError("provider does not exist")
        for row in rows:
            try:
                count = _reset_store_provider(row["path"], provider)
            except RateLimitResetError:
                results.append({"profile": row["name"], "reset_credentials": 0, "errors": [provider]})
                continue
            results.append({"profile": row["name"], "reset_credentials": count, "errors": []})
    else:
        # Every profile resets only its own store. The shared root store is
        # reset like any other profile row; named profiles never receive
        # entries copied from it. Reset preserves every credential value and
        # never copies credentials between profiles.
        for row in rows:
            count, errors = _reset_profile_rows([row])
            results.append({"profile": row["name"], "reset_credentials": count, "errors": errors})

    cache_cleared = False
    try:
        cache_cleared = runtime.clear_codex_usage_cache()
    except RuntimeUnavailable:
        if results:
            results[0]["errors"].append("codex-cache")

    failed = [item["profile"] for item in results if item["errors"]]
    return {
        "ok": not failed,
        "scope": scope,
        "profile": profile_name,
        "provider": (provider_id or "").strip().lower() if scope == "provider" else None,
        "results": results,
        "reset_credentials": sum(item["reset_credentials"] for item in results),
        "codex_usage_state_cleared": cache_cleared,
    }


class ResetRequest(BaseModel):
    scope: Literal["profile", "all", "provider"]
    profile: Optional[str] = None
    provider: Optional[str] = None


def _configured_provider_ids() -> set[str]:
    """Provider IDs that are usable in the current Hermes setup.

    A provider counts as configured when any live profile store (the root
    store or a named profile's store) carries a singleton auth block or a
    non-empty credential-pool entry for it, or when the provider is
    keyless (usable without stored credentials, e.g. OpenCode Free).
    This drives the visibility layer: configured-but-quota-less providers
    are shown as profile-only rows; providers with no credentials at all
    are skipped entirely.
    """
    configured: set[str] = set()
    for row in _profile_rows():
        store = runtime.read_auth_store(row["path"])
        if not isinstance(store, dict):
            continue
        providers = store.get("providers")
        if isinstance(providers, dict):
            for provider_id, state in providers.items():
                if isinstance(state, dict) and _provider_state_has_auth(store, provider_id):
                    configured.add(str(provider_id).strip().lower())
        pool = store.get("credential_pool")
        if isinstance(pool, dict):
            for provider_id, entries in pool.items():
                if isinstance(entries, list) and entries:
                    configured.add(str(provider_id).strip().lower())
    for spec in _current_providers():
        if getattr(spec, "keyless", False):
            configured.add(spec.id)
    return configured


def _build_provider_overview(
    profile_cards: list[dict[str, Any]],
    specs: tuple[Any, ...],
    provider_cards: dict[str, dict[str, Any]],
    *,
    effective_settings: Optional[dict[str, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Group live profile cards under their configured provider.

    Provider buckets are now the primary dashboard layout: every loaded
    provider gets one bucket that preserves its safe quota snapshot and lists
    the profiles that currently use it. Unknown profile providers are kept as
    profile-only buckets so the UI can still explain untracked configurations.

    ``effective_settings`` carries the merged per-provider operator settings
    (defaults + overrides). When omitted the buckets still render — UI code
    treats a missing settings block as "all defaults".
    """
    specs_by_id = {spec.id: spec for spec in specs}
    order_by_id = {spec.id: index for index, spec in enumerate(specs)}
    has_quota_by_id = {
        spec.id: bool(getattr(spec, "has_quota", True)) for spec in specs
    }
    configured_ids = _configured_provider_ids()
    overview: dict[str, dict[str, Any]] = {
        spec.id: {
            "id": spec.id,
            "label": spec.label,
            "provider": _safe_provider_snapshot(provider_cards.get(spec.id)),
            "provider_availability": _provider_availability(provider_cards.get(spec.id)),
            "has_quota": has_quota_by_id.get(spec.id, False),
            "configured": spec.id in configured_ids,
            "profiles": [],
        }
        for spec in specs
    }
    for card in profile_cards:
        provider_id = card.get("provider") or ""
        if not provider_id:
            continue
        spec = specs_by_id.get(provider_id)
        label = spec.label if spec is not None else provider_id
        bucket = overview.setdefault(
            provider_id,
            {
                "id": provider_id,
                "label": label,
                "provider": _safe_provider_snapshot(provider_cards.get(provider_id)),
                "provider_availability": _provider_availability(provider_cards.get(provider_id)),
                "has_quota": has_quota_by_id.get(provider_id, False),
                "configured": provider_id in configured_ids,
                "profiles": [],
            },
        )
        bucket["profiles"].append(
            {
                "id": card.get("id"),
                "profile": card.get("profile"),
                "model": card.get("model"),
                "status": card.get("status"),
                "status_label": card.get("status_label"),
                "reset_at": card.get("reset_at"),
            }
        )

    for bucket in overview.values():
        bucket["profiles"].sort(
            key=lambda item: (
                0 if str(item.get("profile") or "").lower() == "default" else 1,
                str(item.get("profile") or "").lower(),
            )
        )
        bucket["provider_availability"] = _availability_from_bucket(bucket)
        bucket["reset_at"] = _bucket_reset_at(bucket)
        bucket["settings"] = (effective_settings or {}).get(bucket["id"], _settings.effective({}, {}))
        bucket["alert"] = bucket_alert(
            bucket.get("provider"),
            primary_balance_only=_balance_only(bucket.get("provider")),
        )
    return [
        overview[key]
        for key in sorted(
            overview,
            key=lambda provider_id: (
                order_by_id.get(provider_id, len(order_by_id)),
                provider_id,
            ),
        )
    ]


def _bucket_reset_at(bucket: dict[str, Any]) -> Optional[str]:
    """Earliest reset time across the provider's profiles (ISO, UTC).

    The availability row shows this so the operator sees when a
    rate-limited provider recovers without opening every profile.
    """
    reset_times = [
        str(item.get("reset_at"))
        for item in bucket.get("profiles") or []
        if isinstance(item, dict) and item.get("reset_at")
    ]
    if not reset_times:
        return None
    try:
        return min(reset_times)
    except TypeError:
        return None


def _availability_from_bucket(bucket: dict[str, Any]) -> dict[str, str]:
    """Derive provider availability from the profiles actually using it.

    A provider whose credential is exhausted or failing auth must read
    as unavailable/limited even when its usage API still answers (e.g.
    Codex Plus returns plan data while the default profile's key is
    rate-limited). Profile statuses win over the snapshot status:
    any rate_limited profile -> rate_limited, any auth_failed -> auth_failed,
    otherwise fall back to the snapshot status. Labels reuse the exact
    profile status vocabulary so the availability row and profile rows
    read consistently.
    """
    statuses = {
        str(item.get("status") or "").strip().lower()
        for item in bucket.get("profiles") or []
    }
    if not bucket.get("has_quota", False):
        # Catalog-only identity row: no quota adapter exists yet, so the
        # provider reads as "No quota source" regardless of profile state.
        return {"status": "profile_only", "status_label": _STATUS_LABELS["profile_only"]}
    if "rate_limited" in statuses:
        return {"status": "rate_limited", "status_label": _STATUS_LABELS["rate_limited"]}
    if "auth_failed" in statuses:
        return {"status": "auth_failed", "status_label": _STATUS_LABELS["auth_failed"]}
    if "degraded" in statuses:
        return {"status": "degraded", "status_label": _STATUS_LABELS["degraded"]}
    card = bucket.get("provider")
    if not isinstance(card, dict):
        return {"status": "unconfigured", "status_label": _STATUS_LABELS["unconfigured"]}
    if card.get("status") == "ok":
        return {"status": "ready", "status_label": _STATUS_LABELS["ready"]}
    return {"status": "unavailable", "status_label": _STATUS_LABELS["unavailable"]}


def _provider_availability(card: Optional[dict[str, Any]]) -> dict[str, str]:
    """Derive the provider-level availability row shown above its profiles.

    The provider itself is usable when a snapshot exists with status ``ok``.
    A missing snapshot means no credential/response was available, so the
    row reads ``Not configured``; any other status reads ``Unavailable``.
    This is separate from per-profile status: a provider can be available
    while an individual profile on it is rate-limited.
    """
    if not isinstance(card, dict):
        return {"status": "unconfigured", "status_label": _STATUS_LABELS["unconfigured"]}
    if card.get("status") == "ok":
        return {"status": "ready", "status_label": _STATUS_LABELS["ready"]}
    return {"status": "unavailable", "status_label": _STATUS_LABELS["unavailable"]}


def _safe_provider_snapshot(card: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Return only the safe provider fields the combined UI renders.

    The card already carries ``role`` and ``level`` annotations on every
    window/balance item (see ``_annotate_cards``). The annotation step is
    the single source of truth for the alert layer; the safe snapshot is a
    field whitelist, not a re-computation site, so we forward whatever
    ``_annotate_cards`` produced.
    """
    if not isinstance(card, dict):
        return None
    windows = card.get("windows")
    balances = card.get("balances")
    return {
        "id": card.get("id"),
        "label": card.get("label"),
        "status": card.get("status"),
        "plan": card.get("plan"),
        "windows": windows if isinstance(windows, list) else [],
        "balances": balances if isinstance(balances, list) else [],
        "notice": card.get("notice"),
    }


def _balance_only(card: Optional[dict[str, Any]]) -> bool:
    """Return True when the provider has no windows and at least one balance.

    Balance-only providers (currently DeepSeek) treat their balance as the
    primary source. Without this rule the bucket would silently read as
    "no primary items" and stay green even when the wallet is drained.
    """
    if not isinstance(card, dict):
        return False
    windows = card.get("windows")
    balances = card.get("balances")
    windows_present = isinstance(windows, list) and any(
        isinstance(item, dict) for item in windows
    )
    balances_present = isinstance(balances, list) and any(
        isinstance(item, dict) for item in balances
    )
    return not windows_present and balances_present


def _annotate_cards(
    cards: dict[str, dict[str, Any]],
    effective: dict[str, dict[str, Any]],
) -> None:
    """Stamp role/level annotations onto every fetched provider card.

    The annotation mutates the card in place because the same dict is
    forwarded to ``_safe_provider_snapshot`` and ``_build_provider_overview``
    after this step runs. ``effective`` is the merged per-provider
    settings map: each entry carries the thresholds and flags the alert
    layer needs to compute levels.
    """
    for provider_id, card in cards.items():
        if not isinstance(card, dict):
            continue
        settings = effective.get(provider_id) or {}
        annotate_items(
            card,
            window_threshold=settings.get("window_low_percent"),
            balance_threshold=settings.get("balance_low_amount"),
            balance_exhausted_at_zero=settings.get("balance_exhausted_at_zero"),
            primary_balance_only=_balance_only(card),
        )


def _bucket_alerts(overview: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Aggregate bucket-level alert inputs for the top alerts.

    Returns ``{"low": [...], "exhausted": [...]}`` where each entry is a
    provider descriptor carrying the bucket label, the source label, the
    alert level, the percentage (for windows) or amount (for balances),
    and the reset timestamp. The frontend renders the yellow alert from
    ``low`` and the red one from ``exhausted``.
    """
    low: list[dict[str, Any]] = []
    exhausted: list[dict[str, Any]] = []
    for bucket in overview:
        if not isinstance(bucket, dict):
            continue
        alert = bucket.get("alert") or {}
        label = str(bucket.get("label") or bucket.get("id") or "")
        for item in alert.get("low_providers") or []:
            low.append({"provider": label, **item})
        for item in alert.get("exhausted_providers") or []:
            exhausted.append({"provider": label, **item})
    return {"low": low, "exhausted": exhausted}


def _build_summary() -> dict[str, Any]:
    """Fetch configured provider cards concurrently in registry order.

    Only adapters with a real quota source are fetched; catalog-only
    (profile-only) providers stay snapshot-less and render their identity
    row plus assigned profiles.
    """
    cards: dict[str, dict[str, Any]] = {}
    context = _build_context()
    specs = _current_providers()
    quota_specs = [spec for spec in specs if getattr(spec, "has_quota", True)]
    with ThreadPoolExecutor(
        max_workers=max(1, len(quota_specs)),
        thread_name_prefix="usage-provider",
    ) as pool:
        futures = {
            pool.submit(spec.fetch, context): spec
            for spec in quota_specs
        }
        for future in as_completed(futures):
            spec = futures[future]
            try:
                card = future.result()
            except Exception:
                _log_unavailable(spec.id)
                card = None
            if card is not None:
                cards[spec.id] = card
    profile_cards = _build_model_cards()
    raw = _settings.load_raw()
    effective = _settings.effective_view(
        raw.get("defaults", {}),
        raw.get("providers", {}),
        provider_ids=tuple(spec.id for spec in specs),
    )
    # The alert layer annotates every window/balance item with
    # its role/level using the effective thresholds. The annotation runs
    # once per summary build so the 30s cache never serves a stale level.
    _annotate_cards(cards, effective)
    provider_cards = [
        cards[spec.id]
        for spec in specs
        if spec.id in cards
    ]
    overview = _build_provider_overview(
        profile_cards,
        specs,
        cards,
        effective_settings=effective,
    )
    alerts = _bucket_alerts(overview)
    return {
        "version": _plugin_version(),
        "latest_release": _latest_release(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "profiles": profile_cards,
        "providers": provider_cards,
        "provider_overview": overview,
        "alerts": alerts,
        "settings": {
            "defaults": raw.get("defaults", {}),
            "providers": raw.get("providers", {}),
            "effective": effective,
            "fields": list(_settings.known_fields()),
            "schema": {
                "note_max_length": _settings.note_max_length(),
            },
            "storage_path": str(_settings.storage_path()),
        },
    }


def _cached_summary() -> dict[str, Any]:
    global _CACHE, _CACHE_AT
    now = time.monotonic()
    with _CACHE_LOCK:
        if _CACHE is not None and now - _CACHE_AT < _CACHE_TTL_SECONDS:
            return _CACHE
    # Keep cache misses single-flight without holding _CACHE_LOCK across the
    # provider HTTP calls. A reset or another request may invalidate the cache
    # while the build is in progress; re-check once the build lock is held.
    with _CACHE_BUILD_LOCK:
        with _CACHE_LOCK:
            now = time.monotonic()
            if _CACHE is not None and now - _CACHE_AT < _CACHE_TTL_SECONDS:
                return _CACHE
        fresh = _build_summary()
        with _CACHE_LOCK:
            _CACHE = fresh
            _CACHE_AT = time.monotonic()
        return fresh


@router.get("/summary")
async def usage_summary() -> dict[str, Any]:
    """Return the safe, minimal provider summary used by the dashboard tab."""
    return await asyncio.to_thread(_cached_summary)


@router.post("/reset")
async def reset_rate_limits(
    request: Request, payload: ResetRequest
) -> dict[str, Any]:
    """Clear cached provider exhaustion state for one or every live profile."""
    origin = request.headers.get("origin")
    if origin:
        try:
            origin_parts = urlsplit(origin)
            host = request.headers.get("host", "").split(":", 1)[0].lower()
            if (
                origin_parts.scheme not in {"http", "https"}
                or not origin_parts.hostname
                or origin_parts.hostname.lower() != host
            ):
                raise HTTPException(status_code=403, detail="cross-origin request")
        except HTTPException:
            raise
        except ValueError:
            raise HTTPException(status_code=403, detail="cross-origin request")

    profile_name: Optional[str] = None
    provider_id: Optional[str] = None
    if payload.scope == "profile":
        try:
            profile_name = runtime.normalize_profile_name(payload.profile or "")
        except (RuntimeUnavailable, ValueError):
            raise HTTPException(status_code=400, detail="invalid profile") from None
        if profile_name not in {row["name"] for row in _profile_rows()}:
            raise HTTPException(status_code=404, detail="profile not found")
    elif payload.scope == "provider":
        provider_id = (payload.provider or "").strip().lower()
        if provider_id not in _reset_providers():
            raise HTTPException(status_code=404, detail="provider not found")
    try:
        result = await asyncio.to_thread(_reset_profiles, payload.scope, profile_name, provider_id)
    except RateLimitResetError as exc:
        # Keep implementation details (paths, provider errors, tokens) out of
        # the browser response and logs.
        log.warning("usage rate-limit reset failed")
        raise HTTPException(status_code=500, detail=str(exc)) from None
    global _CACHE, _CACHE_AT
    with _CACHE_LOCK:
        _CACHE = None
        _CACHE_AT = 0.0
    result["summary"] = await asyncio.to_thread(_cached_summary)
    return result


@router.get("/settings")
async def get_settings() -> dict[str, Any]:
    """Return the operator-editable settings for this plugin.

    The response carries the raw on-disk layers (``defaults`` and
    ``providers``), the merged effective view per provider, the canonical
    field list, and the on-disk path so the UI can show the source of
    truth. Credentials and storage paths for credential material are never
    included — only the operator fields.
    """
    raw = _settings.load_raw()
    specs = _current_providers()
    effective = _settings.effective_view(
        raw.get("defaults", {}),
        raw.get("providers", {}),
        provider_ids=tuple(spec.id for spec in specs),
    )
    return {
        "defaults": raw.get("defaults", {}),
        "providers": raw.get("providers", {}),
        "effective": effective,
        "fields": list(_settings.known_fields()),
        "schema": {"note_max_length": _settings.note_max_length()},
        "storage_path": str(_settings.storage_path()),
    }


@router.put("/settings")
async def put_settings(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist operator settings.

    Unknown top-level keys, unknown field names, out-of-range thresholds,
    multi-line notes, and notes longer than 120 characters are rejected with
    HTTP 400. The storage file is rewritten atomically (write-temp +
    os.replace under a process-local lock) and the summary cache is
    invalidated so the next read returns the new effective view.
    """
    origin = request.headers.get("origin")
    if origin:
        try:
            origin_parts = urlsplit(origin)
            host = request.headers.get("host", "").split(":", 1)[0].lower()
            if (
                origin_parts.scheme not in {"http", "https"}
                or not origin_parts.hostname
                or origin_parts.hostname.lower() != host
            ):
                raise HTTPException(status_code=403, detail="cross-origin request")
        except HTTPException:
            raise
        except ValueError:
            raise HTTPException(status_code=403, detail="cross-origin request")
    try:
        cleaned = await asyncio.to_thread(_settings.save, payload)
    except SettingsValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    global _CACHE, _CACHE_AT
    with _CACHE_LOCK:
        _CACHE = None
        _CACHE_AT = 0.0
    specs = _current_providers()
    effective = _settings.effective_view(
        cleaned.get("defaults", {}),
        cleaned.get("providers", {}),
        provider_ids=tuple(spec.id for spec in specs),
    )
    return {
        "defaults": cleaned.get("defaults", {}),
        "providers": cleaned.get("providers", {}),
        "effective": effective,
        "fields": list(_settings.known_fields()),
        "schema": {"note_max_length": _settings.note_max_length()},
        "storage_path": str(_settings.storage_path()),
    }
