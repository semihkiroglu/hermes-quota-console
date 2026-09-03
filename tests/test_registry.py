"""Registry discovery tests: scope, determinism, deduplication, fail-closed."""

from __future__ import annotations

import sys
from pathlib import Path

# The dashboard plugin is a standalone directory without a package
# ``__init__.py``; expose the repository root so tests can import it.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from importlib import metadata

import pytest

import dashboard.providers.registry as registry_mod
from dashboard.providers.base import ProviderSpec


BUILTIN_IDS = ("deepseek", "openai-codex", "minimax")


def _entry_points(entries):
    """Build a fake ``metadata.entry_points`` for the provider group."""
    group = registry_mod._ENTRY_POINT_GROUP

    def fake(group=None):
        if group != registry_mod._ENTRY_POINT_GROUP:
            return []
        return [
            metadata.EntryPoint(
                name=name,
                value=value,
                group=registry_mod._ENTRY_POINT_GROUP,
            )
            for name, value in entries
        ]

    return fake


@pytest.fixture
def no_entry_points(monkeypatch):
    monkeypatch.setattr(metadata, "entry_points", _entry_points([]))
    return None


def test_builtin_scope_and_order_without_allowlist(no_entry_points):
    registry = registry_mod._build_registry(None)
    assert tuple(spec.id for spec in registry) == BUILTIN_IDS
    assert all(isinstance(spec, ProviderSpec) for spec in registry)


def test_load_defaults_to_builtin_scope(no_entry_points, monkeypatch):
    # Simulate an environment without Hermes config: allowlist stays None.
    monkeypatch.setattr(registry_mod, "_load_allowlist", lambda: None)
    registry = registry_mod.load()
    assert tuple(spec.id for spec in registry) == BUILTIN_IDS


def test_allowlist_filters_builtins(no_entry_points):
    registry = registry_mod._build_registry(("minimax",))
    assert tuple(spec.id for spec in registry) == ("minimax",)


def test_empty_allowlist_disables_every_provider(no_entry_points):
    registry = registry_mod._build_registry(())
    assert registry == ()


def test_allowlist_ids_are_normalized(no_entry_points):
    registry = registry_mod._build_registry((" DeepSeek ", "OPENAI-CODEX"))
    assert tuple(spec.id for spec in registry) == ("deepseek", "openai-codex")


def test_external_entry_point_loaded_via_allowlist(monkeypatch):
    monkeypatch.setattr(
        metadata,
        "entry_points",
        _entry_points([("fixture", "tests.fixtures.fixture_provider:SPEC")]),
    )
    registry = registry_mod._build_registry(("fixture",))
    assert tuple(spec.id for spec in registry) == ("fixture",)


def test_external_entry_point_joins_builtin_order(monkeypatch):
    monkeypatch.setattr(
        metadata,
        "entry_points",
        _entry_points([("fixture", "tests.fixtures.fixture_provider:SPEC")]),
    )
    registry = registry_mod._build_registry(("fixture", "deepseek"))
    assert tuple(spec.id for spec in registry) == ("deepseek", "fixture")


def test_external_entry_point_requires_explicit_allowlist(monkeypatch):
    monkeypatch.setattr(
        metadata,
        "entry_points",
        _entry_points([("fixture", "tests.fixtures.fixture_provider:SPEC")]),
    )
    # No allowlist: the external adapter must not be discovered implicitly.
    assert tuple(spec.id for spec in registry_mod._build_registry(None)) == BUILTIN_IDS
    # Allowlist without the entry point: still not loaded.
    assert tuple(spec.id for spec in registry_mod._build_registry(("minimax",))) == (
        "minimax",
    )


def test_malformed_entry_point_fails_closed(monkeypatch, caplog):
    monkeypatch.setattr(
        metadata,
        "entry_points",
        _entry_points(
            [
                ("broken", "no.such.module.anywhere:SPEC"),
                ("fixture", "tests.fixtures.fixture_provider:SPEC"),
            ]
        ),
    )
    registry = registry_mod._build_registry(("deepseek", "broken", "fixture"))
    assert tuple(spec.id for spec in registry) == ("deepseek", "fixture")
    # The failure is logged generically; no module path or exception detail.
    messages = " ".join(record.message for record in caplog.records)
    assert "skipped" in messages
    assert "no.such.module" not in messages


def test_entry_point_with_missing_attribute_fails_closed(monkeypatch):
    monkeypatch.setattr(
        metadata,
        "entry_points",
        _entry_points([("fixture", "tests.fixtures.fixture_provider:missing_attr")]),
    )
    registry = registry_mod._build_registry(("fixture",))
    assert registry == ()


def test_entry_point_with_non_spec_value_fails_closed(monkeypatch):
    monkeypatch.setattr(
        metadata,
        "entry_points",
        _entry_points([("fixture", "tests.fixtures.fixture_provider:NOT_A_SPEC")]),
    )
    registry = registry_mod._build_registry(("fixture",))
    assert registry == ()


def test_entry_point_id_mismatch_fails_closed(monkeypatch):
    monkeypatch.setattr(
        metadata,
        "entry_points",
        _entry_points([("renamed", "tests.fixtures.fixture_provider:SPEC")]),
    )
    registry = registry_mod._build_registry(("renamed",))
    assert registry == ()


def test_invalid_entry_point_id_is_skipped(monkeypatch):
    monkeypatch.setattr(
        metadata,
        "entry_points",
        _entry_points(
            [
                ("Bad ID!", "tests.fixtures.fixture_provider:SPEC"),
                ("fixture", "tests.fixtures.fixture_provider:SPEC"),
            ]
        ),
    )
    registry = registry_mod._build_registry(("fixture",))
    assert tuple(spec.id for spec in registry) == ("fixture",)


def test_invalid_entry_point_module_path_is_skipped(monkeypatch, caplog):
    monkeypatch.setattr(
        metadata,
        "entry_points",
        _entry_points([("fixture", "../relative/escape:SPEC")]),
    )
    registry = registry_mod._build_registry(("fixture",))
    assert registry == ()
    messages = " ".join(record.message for record in caplog.records)
    assert "../relative/escape" not in messages


def test_duplicate_external_entry_points_deduplicate(monkeypatch):
    monkeypatch.setattr(
        metadata,
        "entry_points",
        _entry_points(
            [
                ("fixture", "tests.fixtures.fixture_provider:SPEC"),
                ("fixture", "tests.fixtures.fixture_provider:NOT_A_SPEC"),
            ]
        ),
    )
    registry = registry_mod._build_registry(("fixture",))
    # The first sorted occurrence wins; the malformed duplicate is ignored.
    assert tuple(spec.id for spec in registry) == ("fixture",)


def test_builtin_adapter_wins_over_external_duplicate(monkeypatch):
    monkeypatch.setattr(
        metadata,
        "entry_points",
        _entry_points(
            [("deepseek", "tests.fixtures.fixture_provider:DEEPSEEK_LOOKALIKE")]
        ),
    )
    registry = registry_mod._build_registry(("deepseek",))
    assert tuple(spec.id for spec in registry) == ("deepseek",)
    assert registry[0].fetch is registry_mod._BUILTIN_SPECS[0].fetch


def test_discovery_is_deterministic(monkeypatch):
    entries = [
        ("zeta", "tests.fixtures.fixture_provider:ZETA_SPEC"),
        ("alpha", "tests.fixtures.fixture_provider:ALPHA_SPEC"),
    ]
    monkeypatch.setattr(metadata, "entry_points", _entry_points(entries))
    first = registry_mod._build_registry(("alpha", "zeta"))
    second = registry_mod._build_registry(("alpha", "zeta"))
    assert tuple(spec.id for spec in first) == ("alpha", "zeta")
    assert tuple(spec.id for spec in second) == tuple(spec.id for spec in first)


def test_load_applies_allowlist_from_config(monkeypatch):
    monkeypatch.setattr(
        metadata,
        "entry_points",
        _entry_points([("fixture", "tests.fixtures.fixture_provider:SPEC")]),
    )
    monkeypatch.setattr(registry_mod, "_load_allowlist", lambda: ("fixture",))
    registry = registry_mod.load()
    assert tuple(spec.id for spec in registry) == ("fixture",)


def test_fixture_provider_returns_safe_card_schema(monkeypatch):
    monkeypatch.setattr(
        metadata,
        "entry_points",
        _entry_points([("fixture", "tests.fixtures.fixture_provider:SPEC")]),
    )
    fixture = registry_mod._build_registry(("fixture",))
    assert len(fixture) == 1
    card = fixture[0].fetch(context=None)
    assert card == {
        "id": "fixture",
        "label": "Fixture Provider",
        "status": "ok",
        "plan": None,
        "windows": [],
        "balances": [],
        "notice": None,
    }


def test_plugin_api_exposes_registry_order():
    """Load plugin_api.py the way Hermes does and check the provider order.

    The registry remains the sole assembly point: the API must expose the
    built-in adapters in the stable card order without any provider-name
    coupling in the frontend.
    """
    import importlib.util

    api_path = _REPO_ROOT / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("usages.plugin_api", api_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["usages.plugin_api"] = module
    try:
        spec.loader.exec_module(module)
        assert tuple(spec_.id for spec_ in module.PROVIDERS) == BUILTIN_IDS
    finally:
        sys.modules.pop("usages.plugin_api", None)
