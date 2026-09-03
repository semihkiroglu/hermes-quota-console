# Adding a Provider

Step-by-step guide for adding a new **quota provider** (an adapter showing
real balance/plan/quota data) to quota-console. Process, commit standards,
and the pre-PR gate live in [CONTRIBUTING.md](../CONTRIBUTING.md); writing
tests is covered in [WRITING_TESTS.md](WRITING_TESTS.md).

## 1. First answer: does this provider really expose quota data?

Two-layer rule:

- **Identity/profile layer**: *every* provider in the Hermes catalog already
  appears automatically — with profile assignments, state, and reset action.
  **You do not need to write anything for this.** `registry.py::_catalog_extension_specs`
  does it at runtime.
- **Quota layer**: an adapter is only written when a **verified** server-side
  source exists.

Verify before writing an adapter:

1. Does the provider have an official, documented balance/quota API? (e.g.
   DeepSeek `/user/balance`.)
2. Does Hermes already offer an account-usage helper for this provider?
   (Today: `nous`, `openai-codex`, `anthropic`, `openrouter` —
   `fetch_account_usage` in `runtime.py`.) If yes, the adapter uses it and you
   do not write your own HTTP call.
3. Can authentication come from an **API key / OAuth token** in the Hermes
   credential store? If not (e.g. a console only readable through a browser
   session/cookie) → **do not write an adapter**; the provider stays
   `profile-only`. Browser-session scraping is forbidden.

If any of the three is unclear: no adapter; `profile-only` is the correct
behavior. Invented balances, hardcoded plan names, or estimated limits are
**never** shown.

## 2. Create the adapter file: `dashboard/providers/<id>.py`

The simplest real example is `deepseek.py` (single GET, balances). Structure:

```python
"""<Provider> quota adapter (short description)."""

from __future__ import annotations

from typing import Any, Optional

from .base import ProviderContext, ProviderSpec, finite_number


_PROVIDER_ID = "<slug>"       # registry ID: ^[a-z][a-z0-9-]{0,63}$
_LABEL = "<Display Name>"     # name shown in the UI


def fetch(context: ProviderContext) -> Optional[dict[str, Any]]:
    # 1) No credentials -> do not fetch, return None (no card; profile row stays).
    credentials = context.runtime_credentials(_PROVIDER_ID)
    if credentials is None:
        return None
    token = str(credentials.get("api_key") or "").strip()
    if not token:
        return None

    # 2) HTTP via context.get_json; exception -> generic unavailable.
    try:
        payload = context.get_json("https://api.<provider>.com/...", token)
    except Exception:
        context.log_unavailable(_PROVIDER_ID)
        return context.unavailable(_PROVIDER_ID, _LABEL)

    # 3) Normalize the raw payload into a card (base_card + windows/balances).
    card = context.base_card(_PROVIDER_ID, _LABEL, plan="<only if the API returns it>")
    balances: list[dict[str, Any]] = []
    raw = payload.get("balance_infos")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            amount = finite_number(item.get("total_balance"))
            if amount is None:
                continue
            balances.append({
                "currency": _currency(item.get("currency")),  # never assume
                "amount": round(amount, 2),
            })
    card["balances"] = balances
    return card


def _currency(value: Any) -> Optional[str]:
    """Validate a currency code; None if invalid (UI renders without a symbol)."""
    text = str(value or "").strip().upper()
    if text.isascii() and text.isalnum() and len(text) <= 8:
        return text
    return None


SPEC = ProviderSpec(id=_PROVIDER_ID, label=_LABEL, fetch=fetch)
```

**Rules:**

- `fetch` never raises: no credentials → `None`; any HTTP/parse error →
  `context.unavailable(...)` (generic message; raw error details never reach
  the UI or logs).
- The endpoint URL must be **fixed** inside the adapter; no user-controlled
  URLs.
- Currency, plan name, limits — none are **hardcoded**: if the API does not
  return it, the field stays empty/null.
- Secret values are never logged or written to the card; they only go into the
  HTTP header.
- Two usage patterns to reference: `deepseek.py` (simple balances) and
  `minimax.py` (windows + balances, plan from the API). Example using the
  Hermes account-usage helper: `openai_codex.py`.

## 3. Normalized card contract

The returned card follows the `dashboard/providers/base.py::base_card` shape:

```json
{
  "id": "<slug>",
  "label": "<Display Name>",
  "status": "ok",
  "plan": null,
  "windows": [
    {"label": "5-hour", "unit": "credits", "remaining_percent": 88, "reset_at": "..."}
  ],
  "balances": [
    {"label": "Balance", "currency": "USD", "amount": 12.34}
  ],
  "notice": null
}
```

- `windows[]` → quota/limit windows (`remaining_percent` from the provider or
  derived from `used/total`; `reset_at` normalized with `base.iso_time`).
- `balances[]` → wallet/credits (`amount` via `finite_number`; `"0"` is a
  valid zero and is not dropped).
- `status`: `ok` | `unavailable` (generic). `plan`: only when the API returns
  it.
- The adapter does **not** write `role`/`level` — `annotate_items` (alert
  layer) adds them in the backend; read the roles in the `base.py` docstring
  (windows→primary, balances→fallback, balance-only provider →
  `primary_balance_only`).
- A credit row stays as-is, plain information; no reserved visuals/labels.

## 4. Register in the registry: `dashboard/providers/registry.py`

```python
from .base import ProviderSpec
from .deepseek import SPEC as DEEPSEEK
from .minimax import SPEC as MINIMAX
from .my_provider import SPEC as MY_PROVIDER
from .openai_codex import SPEC as OPENAI_CODEX

# Keep this order aligned with the dashboard's compact card layout.
_BUILTIN_SPECS: tuple[ProviderSpec, ...] = (DEEPSEEK, OPENAI_CODEX, MINIMAX, MY_PROVIDER)
```

Note: the order defines the dashboard card layout; on an ID collision with the
catalog order, `_catalog_extension_specs` places the built-in **above** the
catalog row (built-in wins for the same ID) — nothing else to do.

## 5. Write the test: `tests/test_<id>.py`

Hermes-free, network-free test with a fake `ProviderContext` (pattern:
`tests/test_provider_fixtures.py`, guide: [WRITING_TESTS.md](WRITING_TESTS.md)).
Cases to cover:

- Successful payload → normalized card (fields, zero balance, currency
  normalization).
- No credentials / empty token → `None` (no fetch).
- HTTP exception → `unavailable` card, no raw error details.
- Malformed payload (missing/wrong-typed fields) → no crash, safe card.
- Missing/invalid currency → `None`, no symbol assumption.
- (If applicable) plan not returned by the API → `plan` stays empty.

## 6. Verify the registry order

Built-in adapters define the dashboard card layout by their `_BUILTIN_SPECS`
order in `registry.py`; for the same ID the built-in spec wins over the Hermes
catalog row (`_catalog_extension_specs`). Verify your provider's card position
with the registry tests in the suite.

## 7. Do / don't summary

| ✅ Do | ❌ Don't |
|---|---|
| `ProviderSpec` + `fetch(context)` + registry line + test | Import `hermes_cli`/`agent` packages from a provider (only via `context`) |
| Use the Hermes account-usage helper when one exists | Fetch from unverified endpoints / show invented balances |
| Normalize with `base_card` + `base.py` helpers | Hardcode currency, plan, or limits |
| Return a generic unavailable card | Write raw errors/responses/exceptions to cards or logs |
| Verify registry order with the suite | Scrape browser sessions/cookies |
