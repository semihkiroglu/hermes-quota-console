"""OpenAI Codex subscription usage adapter."""

from __future__ import annotations

import re
from typing import Any, Optional

from .base import ProviderContext, ProviderSpec, finite_number, iso_time, percent


_PROVIDER_ID = "openai-codex"
_LABEL = "OpenAI Codex"


def fetch(context: ProviderContext) -> Optional[dict[str, Any]]:
    if not context.codex_configured():
        return None

    try:
        snapshot = context.fetch_account_usage(_PROVIDER_ID)
    except Exception:
        context.log_unavailable(_PROVIDER_ID)
        return context.unavailable(_PROVIDER_ID, _LABEL)
    if snapshot is None:
        return context.unavailable(_PROVIDER_ID, _LABEL)

    raw_windows = getattr(snapshot, "windows", None)
    if not isinstance(raw_windows, (list, tuple)):
        context.log_unavailable(_PROVIDER_ID)
        return context.unavailable(_PROVIDER_ID, _LABEL)

    plan = str(getattr(snapshot, "plan", "") or "").strip() or None
    card = context.base_card(_PROVIDER_ID, _LABEL, plan=plan)
    seen: set[str] = set()
    for window in raw_windows:
        used = finite_number(getattr(window, "used_percent", None))
        if used is None:
            continue
        label = _window_label(getattr(window, "label", None))
        if label in seen:
            continue
        seen.add(label)
        reset_at = getattr(window, "reset_at", None)
        to_isoformat = getattr(reset_at, "isoformat", None)
        reset_value = to_isoformat() if callable(to_isoformat) else None
        card["windows"].append(
            {
                "label": label,
                "unit": "credits",
                "remaining_percent": percent(100.0 - used),
                "reset_at": iso_time(reset_value),
            }
        )

    credit_balance = _credit_balance(getattr(snapshot, "details", None))
    if credit_balance is not None:
        card["balances"].append(credit_balance)

    if not card["windows"] and not card["balances"]:
        card["notice"] = "No quota window reported."
    return card


def _window_label(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return "Weekly" if "week" in raw else "5-hour"


def _credit_balance(details: Any) -> Optional[dict[str, Any]]:
    """Map Hermes' safe credit detail into the shared balance shape."""
    if not isinstance(details, (list, tuple)):
        return None
    for detail in details:
        text = str(detail or "").strip()
        if text == "Credits balance: unlimited":
            return {"label": "Credits", "unitless": True, "unlimited": True}
        match = re.fullmatch(r"Credits balance:\s*\$(-?(?:\d+(?:\.\d*)?|\.\d+))", text)
        if match:
            try:
                amount = float(match.group(1))
            except ValueError:
                continue
            return {
                "label": "Credits",
                "unitless": True,
                "amount": round(amount, 2),
            }
    # Codex reports balance="0" even when has_credits is false. The shared
    # snapshot omits that zero from its human details, but the dashboard should
    # make the account state explicit instead of hiding the row.
    return {"label": "Credits", "unitless": True, "amount": 0.0}


SPEC = ProviderSpec(id=_PROVIDER_ID, label=_LABEL, fetch=fetch)
