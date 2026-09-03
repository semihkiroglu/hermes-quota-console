"""MiniMax Token Plan quota adapter."""

from __future__ import annotations

import re
from typing import Any, Optional

from .base import ProviderContext, ProviderSpec, iso_time, percent


_PROVIDER_ID = "minimax"
_LABEL = "MiniMax"
_ENDPOINT = "https://www.minimax.io/v1/token_plan/remains"
_BALANCE_ENDPOINT = "https://api.minimax.io/account/query_balance"
# Plan labels only surface when the API actually returns one. We deliberately
# do not fall back to a hardcoded subscription name: a personal plan label
# has no place in a shareable, multi-operator dashboard.
_PLAN_KEYS = (
    "current_subscribe_title",
    "plan_name",
    "plan",
    "subscription_plan",
    "tier",
)


def fetch(context: ProviderContext) -> Optional[dict[str, Any]]:
    credentials = context.runtime_credentials(_PROVIDER_ID)
    if credentials is None:
        return None
    token = str(credentials.get("api_key") or "").strip()
    if not token:
        return None

    try:
        payload = context.get_json(
            _ENDPOINT,
            token,
            headers={"Content-Type": "application/json"},
        )
    except Exception:
        context.log_unavailable(_PROVIDER_ID)
        return context.unavailable(_PROVIDER_ID, _LABEL)

    base_resp = payload.get("base_resp")
    if isinstance(base_resp, dict):
        status_code = _number(base_resp.get("status_code"))
        if status_code is not None and int(status_code) != 0:
            context.log_unavailable(_PROVIDER_ID)
            return context.unavailable(_PROVIDER_ID, _LABEL)

    remains = payload.get("model_remains")
    if not isinstance(remains, list):
        return context.unavailable(_PROVIDER_ID, _LABEL)

    # Token Plan exposes a unified general pool plus optional capability pools.
    # Prefer the unified pool; it is the value represented by MiniMax's own
    # usage bar and covers the requested rolling and weekly windows.
    candidates = [
        item
        for item in remains
        if isinstance(item, dict)
        and str(item.get("model_name") or "").strip().lower() == "general"
    ]
    if not candidates:
        candidates = [item for item in remains if isinstance(item, dict)]
    item = candidates[0] if candidates else None
    if item is None:
        return context.unavailable(_PROVIDER_ID, _LABEL)

    card = context.base_card(
        _PROVIDER_ID,
        _LABEL,
        plan=_plan_name(payload, item),
    )
    interval = _remaining_percent(
        item.get("current_interval_remaining_percent"),
        item.get("current_interval_usage_count"),
        item.get("current_interval_total_count"),
    )
    weekly = _remaining_percent(
        item.get("current_weekly_remaining_percent"),
        item.get("current_weekly_usage_count"),
        item.get("current_weekly_total_count"),
    )
    if interval is not None:
        card["windows"].append(
            {
                "label": "5-hour",
                "unit": "credits",
                "remaining_percent": interval,
                "reset_at": iso_time(item.get("end_time")),
            }
        )
    if weekly is not None:
        card["windows"].append(
            {
                "label": "Weekly",
                "unit": "credits",
                "remaining_percent": weekly,
                "reset_at": iso_time(item.get("weekly_end_time")),
            }
        )
    if not card["windows"]:
        card["notice"] = "No quota window reported."
    # The Token Plan quota endpoint does not include the credit wallet shown
    # in the web console. ``query_balance`` is the API-key endpoint for that
    # wallet; keep it best-effort so a balance API regression does not hide
    # otherwise valid quota windows.
    try:
        balance_payload = context.get_json(
            _BALANCE_ENDPOINT,
            token,
            headers={"Content-Type": "application/json"},
        )
    except Exception:
        balance_payload = None
    credit_balance = _credit_balance(balance_payload)
    if credit_balance is not None:
        card["balances"].append(credit_balance)
    return card


def _plan_name(payload: dict[str, Any], item: dict[str, Any]) -> Optional[str]:
    """Use an API-supplied plan label when one is available.

    Plan names belong to the provider's response, not the dashboard. When
    the API omits a plan label we return ``None`` so the card simply does
    not render one.
    """
    for source in (payload, item):
        for key in _PLAN_KEYS:
            value = source.get(key)
            if isinstance(value, dict):
                value = value.get("name") or value.get("title") or value.get("label")
            text = str(value or "").strip()
            if not text:
                continue
            # Keep the compact tier when the API returns e.g. "Token Plan · Plus".
            text = re.split(r"\s*[·|/]\s*", text)[-1].strip()
            text = re.sub(r"^(?:token|coding)\s+plan\s*[-–—:]\s*", "", text, flags=re.I)
            if text and len(text) <= 80:
                return text
    return None


def _credit_balance(payload: Any) -> Optional[dict[str, Any]]:
    """Map MiniMax's API-key credit wallet into the shared balance shape."""
    if not isinstance(payload, dict):
        return None
    base_resp = payload.get("base_resp")
    if isinstance(base_resp, dict):
        status_code = _number(base_resp.get("status_code"))
        if status_code is not None and int(status_code) != 0:
            return None
    amount = _number(payload.get("credit_balance"))
    if amount is None:
        return None
    return {
        "label": "Credit balance",
        "amount": round(amount, 2),
    }


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _remaining_percent(remaining: Any, used: Any, total: Any) -> Optional[int]:
    direct = percent(remaining)
    if direct is not None:
        return direct
    total_number = _number(total)
    used_number = _number(used)
    if total_number is None or used_number is None or total_number <= 0:
        return None
    return percent((total_number - used_number) / total_number * 100.0)


SPEC = ProviderSpec(id=_PROVIDER_ID, label=_LABEL, fetch=fetch)
