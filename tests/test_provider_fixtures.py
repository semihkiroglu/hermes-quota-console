from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, Callable

import pytest


def _provider(plugin_api, provider_id: str):
    return next(spec for spec in plugin_api.PROVIDERS if spec.id == provider_id)


def _context(
    plugin_api,
    resolve_payload: Callable[[str], dict[str, Any]],
    *,
    fetch_account_usage: Callable[[str], Any] | None = None,
):
    calls: list[str] = []
    unavailable: list[str] = []

    def get_json(url: str, _token: str, *, headers: dict[str, str] | None = None):
        del headers
        calls.append(url)
        payload = resolve_payload(url)
        if payload.get("_fixture_transport") == "unavailable":
            raise OSError("offline fixture")
        return payload

    context = plugin_api.ProviderContext(
        runtime_credentials=lambda _provider_id: {"api_key": "fixture-api-key"},
        get_json=get_json,
        base_card=plugin_api.base_card,
        unavailable=plugin_api.unavailable,
        codex_configured=lambda: True,
        fetch_account_usage=fetch_account_usage or (lambda _provider_id: None),
        log_unavailable=unavailable.append,
    )
    return context, calls, unavailable


@pytest.mark.parametrize("scenario", ["valid", "partial", "malformed", "unavailable"])
def test_deepseek_fixture_payloads_return_safe_cards(plugin_api, load_fixture, scenario):
    payload = load_fixture("providers/deepseek.json")[scenario]
    context, calls, unavailable = _context(plugin_api, lambda _url: payload)

    card = _provider(plugin_api, "deepseek").fetch(context)

    assert card is not None
    assert "fixture-api-key" not in repr(card)
    assert len(calls) == 1
    if scenario == "valid":
        assert card["status"] == "ok"
        assert card["balances"] == [{"currency": "USD", "amount": 12.34}]
    elif scenario == "unavailable":
        assert card["status"] == "unavailable"
        assert unavailable == ["deepseek"]
    else:
        assert card["status"] == "ok"
        assert card["balances"] == []
        assert card["notice"] == "No balance reported."


@pytest.mark.parametrize("scenario", ["valid", "partial", "malformed", "unavailable"])
def test_minimax_fixture_payloads_return_safe_cards(plugin_api, load_fixture, scenario):
    fixture = load_fixture("providers/minimax.json")[scenario]

    def resolve_payload(url: str):
        if fixture.get("_fixture_transport") == "unavailable":
            return fixture
        if url.endswith("/remains"):
            return fixture["quota"]
        if url.endswith("/query_balance"):
            return fixture["balance"]
        raise AssertionError("unexpected MiniMax fixture endpoint")

    context, calls, unavailable = _context(plugin_api, resolve_payload)
    card = _provider(plugin_api, "minimax").fetch(context)

    assert card is not None
    assert "fixture-api-key" not in repr(card)
    if scenario == "valid":
        assert card["status"] == "ok"
        assert card["plan"] == "Plus"
        assert [window["remaining_percent"] for window in card["windows"]] == [88, 50]
        assert card["balances"] == [{"label": "Credit balance", "amount": 18.56}]
        assert len(calls) == 2
    elif scenario == "partial":
        assert card["status"] == "ok"
        # Plan names belong to the API: when the response omits a label the
        # card renders no plan rather than guessing a hardcoded one.
        assert card["plan"] is None
        assert card["windows"] == []
        assert card["balances"] == []
        assert card["notice"] == "No quota window reported."
        assert len(calls) == 2
    else:
        assert card["status"] == "unavailable"


def _account_usage_fixture(fixture: dict[str, Any]) -> Callable[[str], Any]:
    def fetch_account_usage(provider_id: str):
        assert provider_id == "openai-codex"
        if fixture.get("_fixture_transport") == "unavailable":
            return None
        snapshot = fixture["snapshot"]
        raw_windows = snapshot.get("windows")
        if isinstance(raw_windows, list):
            windows = [
                SimpleNamespace(
                    label=item.get("label"),
                    used_percent=item.get("used_percent"),
                    reset_at=(
                        datetime.fromisoformat(item["reset_at"].replace("Z", "+00:00"))
                        if item.get("reset_at")
                        else None
                    ),
                )
                for item in raw_windows
            ]
        else:
            windows = raw_windows
        return SimpleNamespace(
            plan=snapshot.get("plan"),
            windows=windows,
            details=snapshot.get("details"),
        )

    return fetch_account_usage


@pytest.mark.parametrize("scenario", ["valid", "partial", "malformed", "unavailable"])
def test_openai_codex_fixture_payloads_return_safe_cards(plugin_api, load_fixture, scenario):
    fixture = load_fixture("providers/openai_codex.json")[scenario]
    context, calls, unavailable = _context(
        plugin_api,
        lambda _url: (_ for _ in ()).throw(AssertionError("Codex must not perform HTTP directly")),
        fetch_account_usage=_account_usage_fixture(fixture),
    )

    card = _provider(plugin_api, "openai-codex").fetch(context)

    assert card is not None
    assert calls == []
    assert "fixture-api-key" not in repr(card)
    if scenario == "valid":
        assert card["status"] == "ok"
        assert card["plan"] == "Codex Plus"
        assert card["windows"] == [
            {
                "label": "5-hour",
                "unit": "credits",
                "remaining_percent": 75,
                "reset_at": "2026-01-02T03:04:05+00:00",
            }
        ]
        assert card["balances"] == [{"label": "Credits", "unitless": True, "amount": 3.5}]
    elif scenario == "partial":
        assert card["status"] == "ok"
        assert card["windows"] == []
        assert card["balances"] == [{"label": "Credits", "unitless": True, "amount": 0.0}]
        assert card["notice"] is None  # balance present, so no notice
    else:
        assert card["status"] == "unavailable"
