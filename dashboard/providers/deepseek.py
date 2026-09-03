"""DeepSeek pay-as-you-go balance adapter."""

from __future__ import annotations

from typing import Any, Optional

from .base import ProviderContext, ProviderSpec, finite_number


_PROVIDER_ID = "deepseek"
_LABEL = "DeepSeek"


def fetch(context: ProviderContext) -> Optional[dict[str, Any]]:
    credentials = context.runtime_credentials(_PROVIDER_ID)
    if credentials is None:
        return None
    token = str(credentials.get("api_key") or "").strip()
    if not token:
        return None

    try:
        payload = context.get_json("https://api.deepseek.com/user/balance", token)
    except Exception:
        context.log_unavailable(_PROVIDER_ID)
        return context.unavailable(_PROVIDER_ID, _LABEL, plan="Pay-as-you-go")

    card = context.base_card(_PROVIDER_ID, _LABEL, plan="Pay-as-you-go")
    balances: list[dict[str, Any]] = []
    raw_balances = payload.get("balance_infos")
    if isinstance(raw_balances, list):
        for item in raw_balances:
            if not isinstance(item, dict):
                continue
            amount = finite_number(item.get("total_balance"))
            if amount is None:
                continue
            balances.append(
                {
                    "currency": _currency(item.get("currency")),
                    "amount": round(amount, 2),
                }
            )

    # Avoid clutter from zero-value auxiliary currencies, but retain a zero
    # balance when it is the only value returned by the account.
    non_zero = [item for item in balances if item["amount"] != 0]
    card["balances"] = non_zero or balances[:1]
    if payload.get("is_available") is False:
        card["notice"] = "API access is currently unavailable."
    elif not card["balances"]:
        card["notice"] = "No balance reported."
    return card


def _currency(value: Any) -> Optional[str]:
    """Normalize a currency code returned by the API.

    Never assume a currency: if the API omits or returns an invalid
    code, we return None so the UI renders the amount without a
    currency symbol rather than guessing one.
    """
    text = str(value or "").strip().upper()
    if text.isascii() and text.isalnum() and len(text) <= 8:
        return text
    return None


SPEC = ProviderSpec(id=_PROVIDER_ID, label=_LABEL, fetch=fetch)
