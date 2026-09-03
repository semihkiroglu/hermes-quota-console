"""Shared provider contract and response helpers for the quota dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Callable, Literal, Optional


@dataclass(frozen=True)
class ProviderContext:
    """Services shared by provider adapters without importing the plugin API.

    Every callback crosses the Hermes compatibility boundary so adapter
    modules never import Hermes packages directly.
    """

    runtime_credentials: Callable[[str], Optional[dict[str, Any]]]
    get_json: Callable[..., dict[str, Any]]
    base_card: Callable[..., dict[str, Any]]
    unavailable: Callable[..., dict[str, Any]]
    codex_configured: Callable[[], bool]
    fetch_account_usage: Callable[[str], Optional[Any]]
    log_unavailable: Callable[[str], None]


@dataclass(frozen=True)
class ProviderSpec:
    """A provider adapter registered in ``providers/registry.py``.

    ``has_quota`` marks adapters that fetch a real quota snapshot.
    Catalog-only (profile-only) providers keep ``has_quota=False``: they
    participate in the identity/profile layer but never fetch and are
    skipped by reset.
    """

    id: str
    label: str
    fetch: Callable[[ProviderContext], Optional[dict[str, Any]]]
    has_quota: bool = True
    keyless: bool = False


def finite_number(value: Any) -> Optional[float]:
    """Return a finite number, including numeric strings, or ``None``."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def percent(value: Any) -> Optional[int]:
    """Clamp a numeric percentage to the displayable 0..100 range."""
    parsed = finite_number(value)
    if parsed is None:
        return None
    return max(0, min(100, round(parsed)))


def iso_time(value: Any) -> Optional[str]:
    """Normalise an epoch (seconds/milliseconds) or ISO timestamp to UTC."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = finite_number(value)
        if number is None:
            return None
        if abs(number) > 10_000_000_000:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    return None


def base_card(provider_id: str, label: str, *, plan: Optional[str] = None) -> dict[str, Any]:
    """Create the stable, browser-safe card shape."""
    return {
        "id": provider_id,
        "label": label,
        "status": "ok",
        "plan": plan,
        "windows": [],
        "balances": [],
        "notice": None,
    }


def unavailable(provider_id: str, label: str, *, plan: Optional[str] = None) -> dict[str, Any]:
    """Create a generic failure card without returning provider error details."""
    card = base_card(provider_id, label, plan=plan)
    card["status"] = "unavailable"
    card["notice"] = "Could not fetch current data."
    return card


# ---------------------------------------------------------------------------
# Alert layer
# ---------------------------------------------------------------------------
#
# Each normalized window/balance item carries two derived fields the dashboard
# uses to render per-source colors and to compose the top alerts:
#
#   role:  "primary" (plan/subscription you actually consume) or
#          "fallback" (reserve: wallet/credits that kicks in only when
#          primary is exhausted).
#   level: "ok" | "low" | "exhausted" | "unknown".
#
# Levels honour the operator contract: a threshold that is ``None`` means
# "do not fire an alert for that source" — the item stays ``ok`` rather
# than being forced into ``unknown`` or a guessed limit. Defaults:
#
#   windows[]    -> role=primary, level computed against window_low_percent
#   balances[]   -> role=fallback, level computed against balance_low_amount
#   providers that emit ONLY balances (e.g. deepseek) treat every balance
#                  as role=primary via the ``primary_balance_only`` flag.
#
# The bucket alert is driven by PRIMARY sources only. A fallback at
# low/exhausted while primary is ok produces no alert and does not change
# the bucket level — "why would I top up credits while my plan still has
# usage?" Fallback is only consulted when every primary is exhausted
# (the operator then falls onto the reserve).

SourceRole = Literal["primary", "fallback"]
SourceLevel = Literal["ok", "low", "exhausted", "unknown"]


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def level_for_window(
    remaining_percent: Any,
    *,
    threshold: Optional[int],
) -> SourceLevel:
    """Compute the level for one window item.

    ``threshold`` is the operator-configured low-percent (1..100). ``None``
    means "no alert configured" and the level stays ``ok`` so the UI does
    not paint a yellow bar the operator never asked for.
    """
    if threshold is None:
        return "ok"
    numeric = _finite_number(remaining_percent)
    if numeric is None:
        return "unknown"
    # A window at exactly 0% is exhausted; at or below threshold but above
    # 0 is low; above threshold is ok. Anything below zero or above 100 is
    # rejected as unknown so a malformed upstream value never raises a
    # false alert.
    if numeric <= 0:
        return "exhausted"
    if numeric <= threshold:
        return "low"
    if numeric > 100:
        return "unknown"
    return "ok"


def level_for_balance(
    amount: Any,
    *,
    threshold: Optional[float],
    exhausted_at_zero: Optional[bool],
    unlimited: bool = False,
) -> SourceLevel:
    """Compute the level for one balance item.

    ``threshold`` is the operator-configured low-amount in the API-reported
    unit; ``None`` disables low alerts on this balance. ``exhausted_at_zero``
    is an opt-in flag — when ``True`` and the balance is exactly 0, the
    level is ``exhausted``; when ``False`` (or ``None``) zero is just ``ok``
    or ``low`` depending on the threshold. An unlimited balance is always
    ``ok`` (the operator never runs out of a reserve they can refill).
    """
    if unlimited:
        return "ok"
    if exhausted_at_zero is True:
        numeric = _finite_number(amount)
        if numeric is not None and numeric <= 0:
            return "exhausted"
    if threshold is None:
        return "ok"
    numeric = _finite_number(amount)
    if numeric is None:
        return "unknown"
    if numeric <= 0:
        # No exhausted-at-zero opt-in but the balance is non-positive:
        # the operator cannot keep using this balance.
        return "exhausted"
    if numeric <= threshold:
        return "low"
    return "ok"


def annotate_items(
    card: dict[str, Any],
    *,
    window_threshold: Optional[int],
    balance_threshold: Optional[float],
    balance_exhausted_at_zero: Optional[bool],
    primary_balance_only: bool = False,
) -> dict[str, Any]:
    """Return ``card`` with every window/balance item annotated.

    The annotation step is the single source of truth for role/level:
    adapters emit raw snapshots, the API layer passes the effective
    thresholds and this helper stamps ``role`` + ``level`` onto each
    item. Items that already carry explicit ``role``/``level`` keep
    their values (the helper does not overwrite non-null fields).
    """
    if not isinstance(card, dict):
        return card
    windows = card.get("windows")
    if isinstance(windows, list):
        for item in windows:
            if not isinstance(item, dict):
                continue
            if item.get("role") not in ("primary", "fallback"):
                item["role"] = "primary"
            if not item.get("level"):
                item["level"] = level_for_window(
                    item.get("remaining_percent"),
                    threshold=window_threshold,
                )
    balances = card.get("balances")
    if isinstance(balances, list):
        default_role: SourceRole = "primary" if primary_balance_only else "fallback"
        for item in balances:
            if not isinstance(item, dict):
                continue
            if item.get("role") not in ("primary", "fallback"):
                item["role"] = default_role
            if not item.get("level"):
                item["level"] = level_for_balance(
                    item.get("amount"),
                    threshold=balance_threshold,
                    exhausted_at_zero=balance_exhausted_at_zero,
                    unlimited=bool(item.get("unlimited")),
                )
    return card


_LEVEL_PRIORITY: dict[str, int] = {
    "ok": 0,
    "unknown": 1,
    "low": 2,
    "exhausted": 3,
}


def worst_level(levels: list[SourceLevel]) -> SourceLevel:
    """Return the most severe level from ``levels``; default ``ok``."""
    worst: SourceLevel = "ok"
    for level in levels:
        priority = _LEVEL_PRIORITY.get(level, 0)
        if priority > _LEVEL_PRIORITY.get(worst, 0):
            worst = level
    return worst


def bucket_alert(
    card: Optional[dict[str, Any]],
    *,
    primary_balance_only: bool = False,
) -> dict[str, Any]:
    """Compute the alert shape for one provider bucket.

    Returns a dict with:

    - ``level``: the worst level across PRIMARY items, ``ok`` when there
      is no primary item at all (the provider exposes only fallback data
      and therefore cannot drive an alert). When the primary set is
      empty AND ``primary_balance_only`` is True (deepseek case) the
      balance IS the primary — the helper falls back to balance levels.
    - ``low_providers`` / ``exhausted_providers``: lists of primary item
      descriptors that the top alert references by provider label and
      the level name. Fallback items never feed the top alert on their
      own; they only matter when primary is exhausted (then they fold
      into the exhausted set so the operator sees the real remaining
      source they would fall back to).

    The result is safe to serialise: only item labels, percentages and
    reset timestamps survive — no provider IDs, no raw values.
    """
    if not isinstance(card, dict):
        return {"level": "ok", "low_providers": [], "exhausted_providers": []}
    windows = [item for item in (card.get("windows") or []) if isinstance(item, dict)]
    balances = [item for item in (card.get("balances") or []) if isinstance(item, dict)]
    primary_windows = [item for item in windows if item.get("role") == "primary"]
    fallback_windows = [item for item in windows if item.get("role") == "fallback"]
    primary_balances = [item for item in balances if item.get("role") == "primary"]
    fallback_balances = [item for item in balances if item.get("role") == "fallback"]

    # A balance-only provider (deepseek) treats every balance
    # as primary so the user actually gets an alert when their wallet
    # drops below threshold. Without this rule deepseek would silently
    # read as "no primary items" and stay green forever.
    if primary_balance_only:
        primary_balances = primary_balances or balances
        fallback_balances = []

    primary_levels_raw = [
        item.get("level") for item in primary_windows + primary_balances
    ]
    primary_levels: list[SourceLevel] = [
        level for level in primary_levels_raw
        if level in ("ok", "low", "exhausted", "unknown")
    ]
    bucket_level: SourceLevel = worst_level(primary_levels)

    def _describe(item: dict[str, Any]) -> dict[str, Any]:
        remaining = item.get("remaining_percent")
        amount = item.get("amount")
        return {
            "label": str(item.get("label") or ""),
            "level": item.get("level"),
            "remaining_percent": remaining if isinstance(remaining, (int, float)) else None,
            "amount": amount if isinstance(amount, (int, float)) else None,
            "reset_at": item.get("reset_at"),
        }

    low_providers = [
        _describe(item) for item in primary_windows + primary_balances
        if item.get("level") == "low"
    ]
    exhausted_providers = [
        _describe(item) for item in primary_windows + primary_balances
        if item.get("level") == "exhausted"
    ]
    # When the primary set is fully exhausted the operator falls onto the
    # reserve; surface those fallback items so the top alert names the
    # actual remaining resource instead of a generic "out of quota" line.
    if bucket_level == "exhausted" and not exhausted_providers:
        exhausted_providers = [
            _describe(item) for item in fallback_windows + fallback_balances
            if item.get("level") == "exhausted"
        ]

    return {
        "level": bucket_level,
        "low_providers": low_providers,
        "exhausted_providers": exhausted_providers,
    }
