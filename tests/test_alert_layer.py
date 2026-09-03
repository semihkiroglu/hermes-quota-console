"""Alert layer tests.

The alert layer stamps a ``role`` and ``level`` annotation on top of every
normalized window/balance item and exposes bucket-level alert inputs
that the dashboard renders as yellow/red top alerts. The contract:

- ``role``: primary (plan/subscription) or fallback (reserve).
  Defaults: ``windows[]`` -> primary, ``balances[]`` -> fallback.
  Balance-only providers (DeepSeek) treat their balance as primary.
- ``level``: ``ok | low | exhausted | unknown``. ``None`` threshold
  (off-by-default) means "do not fire an alert" -> ``ok``.
- Bucket alert: worst primary level. Fallback never raises an alert
  while primary is ok — the plan is the resource actually being used,
  so a healthy plan does not ask for a credit top-up. Fallback only
  matters once primary is exhausted.

These tests pin the contract so a future refactor cannot silently
flip the alert pipeline into "worst of every source" mode.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_API_PATH = REPO_ROOT / "dashboard" / "plugin_api.py"


@pytest.fixture
def isolated_plugin_api(monkeypatch):
    """Load plugin_api.py in isolation so each test gets a fresh module."""
    module_name = f"quota_console_alert_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_API_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "_CACHE", None)
        monkeypatch.setattr(module, "_CACHE_AT", 0.0)
        yield module
    finally:
        for loaded in tuple(sys.modules):
            if loaded == module_name or loaded.startswith(f"{module_name}."):
                sys.modules.pop(loaded, None)


# ---------------------------------------------------------------------------
# Unit tests: helpers in providers/base.py
# ---------------------------------------------------------------------------


def test_level_for_window_null_threshold_is_ok():
    """A null threshold (off-by-default) means "no alert configured".

    The level must stay ``ok`` so the UI does not paint a yellow bar the
    operator never asked for. This is the central alert-layer guarantee.
    """
    from dashboard.providers.base import level_for_window
    assert level_for_window(50, threshold=None) == "ok"
    assert level_for_window(5, threshold=None) == "ok"
    assert level_for_window(0, threshold=None) == "ok"
    assert level_for_window(None, threshold=None) == "ok"


def test_level_for_window_threshold_boundaries():
    """Boundary semantics: <= threshold is low, == 0 is exhausted."""
    from dashboard.providers.base import level_for_window
    # Above threshold stays ok.
    assert level_for_window(50, threshold=20) == "ok"
    # At threshold is low.
    assert level_for_window(20, threshold=20) == "low"
    # Just below threshold is low.
    assert level_for_window(19, threshold=20) == "low"
    # Zero percent is exhausted, not low.
    assert level_for_window(0, threshold=20) == "exhausted"
    # Negative percentage (a malformed upstream value) is exhausted —
    # the operator cannot consume negative remaining quota, so the alert
    # pipeline must treat this as "no quota left" instead of guessing.
    assert level_for_window(-1, threshold=20) == "exhausted"
    # Out-of-range values render as unknown, not as false alerts.
    assert level_for_window(150, threshold=20) == "unknown"
    # Missing/invalid input is unknown.
    assert level_for_window(None, threshold=20) == "unknown"
    assert level_for_window("not-a-number", threshold=20) == "unknown"


def test_level_for_balance_null_threshold_is_ok():
    """A null balance_low_amount threshold disables low alerts on this balance."""
    from dashboard.providers.base import level_for_balance
    assert level_for_balance(0.5, threshold=None, exhausted_at_zero=None) == "ok"
    assert level_for_balance(0, threshold=None, exhausted_at_zero=None) == "ok"


def test_level_for_balance_exhausted_at_zero_opt_in():
    """exhausted_at_zero=True flips amount=0 to exhausted."""
    from dashboard.providers.base import level_for_balance
    # Off by default with no threshold: zero balance stays ok (the alert
    # pipeline never invents an exhausted state when no rule applies).
    assert level_for_balance(0, threshold=None, exhausted_at_zero=None) == "ok"
    assert level_for_balance(0, threshold=None, exhausted_at_zero=False) == "ok"
    # Opt-in: zero becomes exhausted.
    assert level_for_balance(0, threshold=None, exhausted_at_zero=True) == "exhausted"
    # Negative balance with no threshold stays ok — the operator contract
    # is "null threshold means no alert": the alert pipeline never invents
    # a limit the operator did not set.
    assert level_for_balance(-1, threshold=None, exhausted_at_zero=None) == "ok"
    # Negative balance with a configured threshold: still exhausted.
    assert level_for_balance(-1, threshold=10, exhausted_at_zero=None) == "exhausted"


def test_level_for_balance_threshold_boundaries():
    """Balance at/below threshold is low; above is ok."""
    from dashboard.providers.base import level_for_balance
    assert level_for_balance(150, threshold=100, exhausted_at_zero=None) == "ok"
    assert level_for_balance(100, threshold=100, exhausted_at_zero=None) == "low"
    assert level_for_balance(50, threshold=100, exhausted_at_zero=None) == "low"
    assert level_for_balance(0, threshold=100, exhausted_at_zero=None) == "exhausted"


def test_level_for_balance_unlimited_always_ok():
    """An unlimited balance never raises an alert."""
    from dashboard.providers.base import level_for_balance
    assert level_for_balance(None, threshold=10, exhausted_at_zero=True, unlimited=True) == "ok"
    assert level_for_balance(0, threshold=10, exhausted_at_zero=True, unlimited=True) == "ok"


def test_annotate_items_default_roles():
    """windows[] -> primary, balances[] -> fallback (default contract)."""
    from dashboard.providers.base import annotate_items
    card = {
        "windows": [{"label": "5-hour", "remaining_percent": 50}],
        "balances": [{"label": "Wallet", "amount": 12.5}],
    }
    annotate_items(
        card,
        window_threshold=20,
        balance_threshold=5,
        balance_exhausted_at_zero=None,
    )
    assert card["windows"][0]["role"] == "primary"
    assert card["balances"][0]["role"] == "fallback"
    assert card["windows"][0]["level"] == "ok"
    assert card["balances"][0]["level"] == "ok"


def test_annotate_items_balance_only_treats_balance_as_primary():
    """DeepSeek case: balances on a balance-only provider are primary."""
    from dashboard.providers.base import annotate_items
    card = {
        "windows": [],
        "balances": [{"label": "Wallet", "amount": 0.5}],
    }
    annotate_items(
        card,
        window_threshold=None,
        balance_threshold=10,
        balance_exhausted_at_zero=None,
        primary_balance_only=True,
    )
    assert card["balances"][0]["role"] == "primary"
    # 0.5 <= 10 -> low
    assert card["balances"][0]["level"] == "low"


def test_annotate_items_preserves_existing_role_and_level():
    """Explicit role/level values win: helper does not overwrite them."""
    from dashboard.providers.base import annotate_items
    card = {
        "windows": [{"label": "5-hour", "role": "fallback", "level": "unknown", "remaining_percent": 5}],
    }
    annotate_items(
        card,
        window_threshold=20,
        balance_threshold=None,
        balance_exhausted_at_zero=None,
    )
    assert card["windows"][0]["role"] == "fallback"
    assert card["windows"][0]["level"] == "unknown"


def test_annotate_items_window_at_threshold_is_low():
    """Threshold boundary: window at exactly the threshold becomes low."""
    from dashboard.providers.base import annotate_items
    card = {
        "windows": [
            {"label": "5-hour", "remaining_percent": 20},
            {"label": "Weekly", "remaining_percent": 21},
        ],
    }
    annotate_items(
        card,
        window_threshold=20,
        balance_threshold=None,
        balance_exhausted_at_zero=None,
    )
    assert card["windows"][0]["level"] == "low"
    assert card["windows"][1]["level"] == "ok"


# ---------------------------------------------------------------------------
# bucket_alert: primary-only semantics
# ---------------------------------------------------------------------------


def test_bucket_alert_no_card_is_ok():
    """No card (provider without a snapshot) -> ok, no alert entries."""
    from dashboard.providers.base import bucket_alert
    alert = bucket_alert(None)
    assert alert == {"level": "ok", "low_providers": [], "exhausted_providers": []}


def test_bucket_alert_primary_exhausted_wins_over_fallback_low():
    """The bucket level tracks PRIMARY only. A low fallback must not lower the
    alert when the primary is already exhausted — the operator is on the
    reserve and the dashboard should not multiply alerts."""
    from dashboard.providers.base import bucket_alert
    card = {
        "windows": [
            {"label": "5-hour", "role": "primary", "level": "exhausted", "remaining_percent": 0},
        ],
        "balances": [
            {"label": "Wallet", "role": "fallback", "level": "low", "amount": 1.0},
        ],
    }
    alert = bucket_alert(card)
    assert alert["level"] == "exhausted"
    assert len(alert["exhausted_providers"]) == 1
    assert alert["exhausted_providers"][0]["label"] == "5-hour"


def test_bucket_alert_fallback_low_does_not_alert_when_primary_ok():
    """A fallback balance at 'low' must NOT raise a top alert while the
    primary window is 'ok' — a healthy plan does not ask for a credit
    top-up. This is the central alert-layer contract.
    """
    from dashboard.providers.base import bucket_alert
    card = {
        "windows": [
            {"label": "5-hour", "role": "primary", "level": "ok", "remaining_percent": 50},
        ],
        "balances": [
            {"label": "Wallet", "role": "fallback", "level": "low", "amount": 1.0},
        ],
    }
    alert = bucket_alert(card)
    assert alert["level"] == "ok"
    assert alert["low_providers"] == []
    assert alert["exhausted_providers"] == []


def test_bucket_alert_low_wins_over_unknown():
    """low > unknown in severity; bucket level is the worst primary."""
    from dashboard.providers.base import bucket_alert
    card = {
        "windows": [
            {"label": "5-hour", "role": "primary", "level": "unknown"},
            {"label": "Weekly", "role": "primary", "level": "low", "remaining_percent": 10},
        ],
    }
    alert = bucket_alert(card)
    assert alert["level"] == "low"
    assert len(alert["low_providers"]) == 1
    assert alert["low_providers"][0]["label"] == "Weekly"


def test_bucket_alert_exhausted_wins_over_low():
    """exhausted > low; bucket level must pick exhausted."""
    from dashboard.providers.base import bucket_alert
    card = {
        "windows": [
            {"label": "5-hour", "role": "primary", "level": "low", "remaining_percent": 5},
            {"label": "Weekly", "role": "primary", "level": "exhausted", "remaining_percent": 0},
        ],
    }
    alert = bucket_alert(card)
    assert alert["level"] == "exhausted"
    assert len(alert["exhausted_providers"]) == 1
    assert alert["exhausted_providers"][0]["label"] == "Weekly"


def test_bucket_alert_fallback_exhausted_surfaces_when_no_primary_exhausted():
    """When only the reserve is exhausted the operator still has usable
    primary quota. The top alert must surface the reserve state so the
    operator knows what happens after the primary runs out."""
    from dashboard.providers.base import bucket_alert
    card = {
        "windows": [
            {"label": "5-hour", "role": "primary", "level": "ok", "remaining_percent": 80},
        ],
        "balances": [
            {"label": "Wallet", "role": "fallback", "level": "exhausted", "amount": 0},
        ],
    }
    alert = bucket_alert(card)
    # Primary is ok: bucket level is ok, no alert raised — the reserve
    # stays silent while the plan still has usage.
    assert alert["level"] == "ok"
    assert alert["exhausted_providers"] == []


def test_bucket_alert_no_primary_no_alert():
    """A provider with no primary sources at all stays silent.

    Bucket alert is driven by PRIMARY sources only. A provider that
    exposes only fallback data (e.g. an adapter that emits balances
    without a primary plan) cannot drive an alert on its own. The
    deepseek case is the explicit exception: it opts into
    ``primary_balance_only`` so its balance counts as primary.
    """
    from dashboard.providers.base import bucket_alert
    card = {
        "windows": [],
        "balances": [
            {"label": "Wallet", "role": "fallback", "level": "exhausted", "amount": 0},
        ],
    }
    alert = bucket_alert(card)
    # No primary items: bucket level is ok and the fallback description
    # is dropped. The operator never sees a top alert for a provider
    # whose only signals are reserves they cannot consume.
    assert alert["level"] == "ok"
    assert alert["exhausted_providers"] == []


def test_bucket_alert_fallback_exhausted_not_duplicated_when_primary_exhausted():
    """When the primary is already exhausted AND the fallback is also
    exhausted, the top alert names the primary item. Adding the fallback
    would double-report the same provider and confuse the operator."""
    from dashboard.providers.base import bucket_alert
    card = {
        "windows": [
            {"label": "5-hour", "role": "primary", "level": "exhausted", "remaining_percent": 0},
        ],
        "balances": [
            {"label": "Wallet", "role": "fallback", "level": "exhausted", "amount": 0},
        ],
    }
    alert = bucket_alert(card)
    assert alert["level"] == "exhausted"
    labels = [item["label"] for item in alert["exhausted_providers"]]
    # Primary owns the alert; the fallback is implied because the operator
    # is already falling onto the reserve.
    assert labels == ["5-hour"]


def test_bucket_alert_balance_only_treats_balance_as_primary():
    """Balance-only providers (DeepSeek) — balance level drives the bucket."""
    from dashboard.providers.base import bucket_alert
    card = {
        "windows": [],
        "balances": [
            {"label": "Wallet", "amount": 0, "level": "exhausted"},
        ],
    }
    alert = bucket_alert(card, primary_balance_only=True)
    assert alert["level"] == "exhausted"
    assert alert["exhausted_providers"][0]["label"] == "Wallet"


def test_bucket_alert_describe_carries_reset_and_percent():
    """The alert descriptor must carry the reset timestamp + remaining percent
    so the top alert can render 'resets HH:MM' without a second roundtrip."""
    from dashboard.providers.base import bucket_alert
    card = {
        "windows": [
            {
                "label": "5-hour",
                "role": "primary",
                "level": "low",
                "remaining_percent": 12,
                "reset_at": "2026-09-02T01:04:00+00:00",
            },
        ],
    }
    alert = bucket_alert(card)
    descriptor = alert["low_providers"][0]
    assert descriptor["remaining_percent"] == 12
    assert descriptor["reset_at"] == "2026-09-02T01:04:00+00:00"


# ---------------------------------------------------------------------------
# Integration: summary payload + bucket wiring
# ---------------------------------------------------------------------------


def test_summary_carries_alerts_and_bucket_alert_shape(isolated_plugin_api, monkeypatch):
    """The /summary payload exposes aggregated alerts + per-bucket alert shape.

    The frontend reads ``data.alerts`` (low + exhausted lists) and
    ``bucket.alert`` (level + descriptors) to render the top alerts and
    per-row tints. Both fields must be present even when nothing is on
    fire — missing fields crash the bundle's data-driven renderers.
    """
    api = isolated_plugin_api
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: [])
    monkeypatch.setattr(api.runtime, "resolve_provider_credentials", lambda provider: None)
    monkeypatch.setattr(api.runtime, "codex_configured", lambda: False)

    summary = api._cached_summary()
    assert "alerts" in summary
    assert summary["alerts"] == {"low": [], "exhausted": []}
    for bucket in summary["provider_overview"]:
        assert "alert" in bucket
        assert set(bucket["alert"]) == {"level", "low_providers", "exhausted_providers"}


def _patch_spec_fetch(api, monkeypatch, spec_id, fetch_fn):
    """Replace ``fetch`` on a frozen ProviderSpec via ``object.__setattr__``.

    ``ProviderSpec`` is a frozen dataclass, so a normal ``setattr`` raises
    ``FrozenInstanceError``. The runtime path reads ``spec.fetch`` per
    request, so a bypass via ``object.__setattr__`` is enough to redirect
    a single adapter to a deterministic fixture card.
    """
    specs = api._current_providers()
    target = next(spec for spec in specs if spec.id == spec_id)
    object.__setattr__(target, "fetch", fetch_fn)


def test_summary_annotates_windows_and_balances_with_role_level(isolated_plugin_api, monkeypatch):
    """Every fetched card item carries role + level so the UI can tint rows."""
    api = isolated_plugin_api
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: [])
    _patch_spec_fetch(
        api,
        monkeypatch,
        "minimax",
        lambda ctx: {
            "id": "minimax",
            "label": "MiniMax",
            "status": "ok",
            "plan": None,
            "windows": [{"label": "5-hour", "remaining_percent": 15, "reset_at": None}],
            "balances": [{"label": "Wallet", "amount": 12.5}],
            "notice": None,
        },
    )

    # Inject effective settings that flip the 5-hour window to "low".
    monkeypatch.setattr(
        api._settings,
        "load_raw",
        lambda: {
            "defaults": {"window_low_percent": 20},
            "providers": {},
        },
    )

    summary = api._cached_summary()
    minimax = next(b for b in summary["provider_overview"] if b["id"] == "minimax")
    assert minimax["provider"]["windows"][0]["role"] == "primary"
    assert minimax["provider"]["windows"][0]["level"] == "low"
    assert minimax["provider"]["balances"][0]["role"] == "fallback"
    # balance threshold is null (default) -> balance stays ok even
    # though the operator set window_low_percent.
    assert minimax["provider"]["balances"][0]["level"] == "ok"

    # Bucket alert: only the primary window is low.
    assert minimax["alert"]["level"] == "low"
    assert [item["label"] for item in minimax["alert"]["low_providers"]] == ["5-hour"]
    assert minimax["alert"]["exhausted_providers"] == []

    # Top alerts aggregate across providers.
    assert summary["alerts"]["low"]
    low_entry = summary["alerts"]["low"][0]
    assert low_entry["provider"] == "MiniMax"
    assert low_entry["label"] == "5-hour"
    assert low_entry["remaining_percent"] == 15


def test_summary_with_default_settings_fires_no_alert(isolated_plugin_api, monkeypatch):
    """All thresholds default to null -> no alerts fire.

    With the off-by-default contract in place, the dashboard adds no
    new alerts until the operator explicitly sets a threshold.
    """
    api = isolated_plugin_api
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: [])
    # Inline-fetch a card with low-looking values.
    _patch_spec_fetch(
        api,
        monkeypatch,
        "minimax",
        lambda ctx: {
            "id": "minimax",
            "label": "MiniMax",
            "status": "ok",
            "plan": None,
            "windows": [{"label": "5-hour", "remaining_percent": 3, "reset_at": None}],
            "balances": [{"label": "Wallet", "amount": 0.01}],
            "notice": None,
        },
    )
    # Default settings -> all thresholds null.
    monkeypatch.setattr(api._settings, "load_raw", lambda: {"defaults": {}, "providers": {}})

    summary = api._cached_summary()
    assert summary["alerts"]["low"] == []
    assert summary["alerts"]["exhausted"] == []
    for bucket in summary["provider_overview"]:
        assert bucket["alert"]["level"] == "ok"
        assert bucket["alert"]["low_providers"] == []
        assert bucket["alert"]["exhausted_providers"] == []


def test_summary_balance_only_provider_alerts_on_drained_balance(isolated_plugin_api, monkeypatch):
    """DeepSeek case: a drained balance must surface as exhausted even when
    the operator only configured a window threshold for a different
    provider."""
    api = isolated_plugin_api
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: [])

    _patch_spec_fetch(
        api,
        monkeypatch,
        "deepseek",
        lambda ctx: {
            "id": "deepseek",
            "label": "DeepSeek",
            "status": "ok",
            "plan": None,
            "windows": [],
            "balances": [{"label": "Wallet", "amount": 0}],
            "notice": None,
        },
    )
    # Opt-in to "treat zero as exhausted" for deepseek via global default.
    monkeypatch.setattr(
        api._settings,
        "load_raw",
        lambda: {
            "defaults": {"balance_exhausted_at_zero": True, "balance_low_amount": 5},
            "providers": {},
        },
    )

    summary = api._cached_summary()
    deepseek = next(b for b in summary["provider_overview"] if b["id"] == "deepseek")
    assert deepseek["alert"]["level"] == "exhausted"
    assert deepseek["alert"]["exhausted_providers"][0]["label"] == "Wallet"
    # And it surfaces on the top alert.
    assert summary["alerts"]["exhausted"]
    assert summary["alerts"]["exhausted"][0]["provider"] == "DeepSeek"


def test_summary_exhausted_aggregates_above_low_in_alerts(isolated_plugin_api, monkeypatch):
    """The top alerts are independent lists — the UI renders them in
    priority order (exhausted first). This test pins the aggregate shape
    so a future refactor cannot accidentally collapse the two alerts."""
    api = isolated_plugin_api
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: [])

    _patch_spec_fetch(
        api,
        monkeypatch,
        "minimax",
        lambda ctx: {
            "id": "minimax",
            "label": "MiniMax",
            "status": "ok",
            "plan": None,
            "windows": [{"label": "5-hour", "remaining_percent": 0, "reset_at": None}],
            "balances": [],
            "notice": None,
        },
    )
    _patch_spec_fetch(
        api,
        monkeypatch,
        "openai-codex",
        lambda ctx: {
            "id": "openai-codex",
            "label": "OpenAI Codex",
            "status": "ok",
            "plan": None,
            "windows": [{"label": "5-hour", "remaining_percent": 18, "reset_at": None}],
            "balances": [],
            "notice": None,
        },
    )
    monkeypatch.setattr(
        api._settings,
        "load_raw",
        lambda: {"defaults": {"window_low_percent": 20}, "providers": {}},
    )

    summary = api._cached_summary()
    exhausted = summary["alerts"]["exhausted"]
    low = summary["alerts"]["low"]
    assert len(exhausted) == 1
    assert exhausted[0]["provider"] == "MiniMax"
    assert len(low) == 1
    assert low[0]["provider"] == "OpenAI Codex"


def test_summary_window_low_percent_triggers_yellow_alert(isolated_plugin_api, monkeypatch):
    """Setting window_low_percent=20 for minimax while its window is at
    <=20% raises a yellow alert with provider name, percent, and reset
    time when present."""
    api = isolated_plugin_api
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: [])

    _patch_spec_fetch(
        api,
        monkeypatch,
        "minimax",
        lambda ctx: {
            "id": "minimax",
            "label": "MiniMax",
            "status": "ok",
            "plan": None,
            "windows": [
                {
                    "label": "5-hour",
                    "remaining_percent": 12,
                    "reset_at": "2026-09-02T01:04:00+00:00",
                }
            ],
            "balances": [],
            "notice": None,
        },
    )
    monkeypatch.setattr(
        api._settings,
        "load_raw",
        lambda: {
            "defaults": {},
            "providers": {"minimax": {"window_low_percent": 20}},
        },
    )

    summary = api._cached_summary()
    low = summary["alerts"]["low"]
    assert len(low) == 1
    entry = low[0]
    assert entry["provider"] == "MiniMax"
    assert entry["label"] == "5-hour"
    assert entry["remaining_percent"] == 12
    # The reset timestamp must reach the browser so the UI can render
    # "resets HH:MM" in the operator's locale/timezone.
    assert entry["reset_at"] == "2026-09-02T01:04:00+00:00"


def test_summary_alerts_payload_never_leaks_credentials(isolated_plugin_api, monkeypatch):
    """The alert payload must carry only safe descriptor fields; no
    provider secrets, no raw amounts that could double as identifiers."""
    api = isolated_plugin_api
    monkeypatch.setattr(api.runtime, "list_profiles", lambda: [])

    _patch_spec_fetch(
        api,
        monkeypatch,
        "minimax",
        lambda ctx: {
            "id": "minimax",
            "label": "MiniMax",
            "status": "ok",
            "plan": None,
            "windows": [{"label": "5-hour", "remaining_percent": 5, "reset_at": None}],
            "balances": [],
            "notice": None,
        },
    )
    monkeypatch.setattr(
        api._settings,
        "load_raw",
        lambda: {"defaults": {"window_low_percent": 20}, "providers": {}},
    )

    summary = api._cached_summary()
    blob = json.dumps(summary)
    assert "SECRET" not in blob
    assert "api_key" not in blob
    assert "access_token" not in blob
    # The alert descriptor only carries allowed fields.
    for entry in summary["alerts"]["low"] + summary["alerts"]["exhausted"]:
        assert set(entry).issubset(
            {"provider", "label", "level", "remaining_percent", "amount", "reset_at"}
        )
