"""Tests for the dashboard bundle's hidden-bucket partition helper.

The "Providers by profile" list renders visible cards
first and hidden cards (auto-hidden or user-hidden) at the bottom while
customize mode is on. The bundle exposes ``partitionBuckets``
at module scope so we can exercise the same function the browser renders
with, without spinning up a fake React SDK.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATH = REPO_ROOT / "dashboard" / "dist" / "index.js"


def _node_call(script: str) -> str:
    completed = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "node script failed: rc=%d stderr=%s" % (completed.returncode, completed.stderr)
        )
    return completed.stdout


def _bundle_loader() -> str:
    return (
        "const fs = require('fs');\n"
        "const path = %r;\n"
        "const code = fs.readFileSync(path, 'utf8');\n"
        "const window = {\n"
        "  __HERMES_PLUGIN_SDK__: null,\n"
        "  __HERMES_PLUGINS__: { register: function () {} },\n"
        "};\n"
        "const fn = new Function('module', 'window', code);\n"
        "const m = { exports: {} };\n"
        "fn(m, window);\n"
    ) % str(BUNDLE_PATH)


def _partition(buckets, choices):
    """Run partitionBuckets(buckets, isHiddenFn) under Node.

    ``choices`` is a dict {bucketId: True/False} mirroring the localStorage
    ``hiddenProviders`` payload used in the dashboard IIFE. When the bucket
    has a choice, it overrides the auto-hidden signal so we can drive both
    branches with a single fixture.
    """
    payload = json.dumps({"buckets": buckets, "choices": choices})
    script = (
        _bundle_loader()
        + "const input = JSON.parse(%r);\n" % payload
        + "const partition = m.exports.partitionBuckets;\n"
        + "const isHidden = function (bucket) {\n"
        + "  const bucketProfiles = Array.isArray(bucket.profiles) ? bucket.profiles : [];\n"
        + "  const hasQuota = Boolean(bucket.has_quota);\n"
        + "  const configured = Boolean(bucket.configured);\n"
        + "  const autoHidden = !hasQuota && !bucketProfiles.length && configured;\n"
        + "  const choice = Object.prototype.hasOwnProperty.call(input.choices, bucket.id)\n"
        + "    ? input.choices[bucket.id]\n"
        + "    : undefined;\n"
        + "  return choice === undefined ? autoHidden : Boolean(choice);\n"
        + "};\n"
        + "const out = partition(input.buckets, isHidden);\n"
        + "process.stdout.write(JSON.stringify({\n"
        + "  visible: out.visible.map(function (b) { return b.id; }),\n"
        + "  hidden: out.hidden.map(function (b) { return b.id; }),\n"
        + "}));\n"
    )
    return json.loads(_node_call(script))


def _apply_order(buckets, order):
    payload = json.dumps({"buckets": buckets, "order": order})
    script = (
        _bundle_loader()
        + "const input = JSON.parse(%r);\n" % payload
        + "const out = m.exports.applyStoredOrder(input.buckets, input.order);\n"
        + "process.stdout.write(JSON.stringify(out.map(function (b) { return b.id; })));\n"
    )
    return json.loads(_node_call(script))


def _move(ids, from_id, to_id, edge=None):
    payload = json.dumps({"ids": ids, "from": from_id, "to": to_id, "edge": edge})
    script = (
        _bundle_loader()
        + "const input = JSON.parse(%r);\n" % payload
        + "process.stdout.write(JSON.stringify(m.exports.moveProviderId(input.ids, input.from, input.to, input.edge)));\n"
    )
    return json.loads(_node_call(script))


def _bucket(provider_id):
    return {"id": provider_id, "label": provider_id, "configured": True, "has_quota": True, "profiles": []}


def test_apply_stored_order_with_empty_order_keeps_backend_order():
    buckets = [_bucket("deepseek"), _bucket("openai-codex"), _bucket("minimax")]
    assert _apply_order(buckets, []) == ["deepseek", "openai-codex", "minimax"]


def test_apply_stored_order_sorts_by_stored_positions():
    buckets = [_bucket("deepseek"), _bucket("openai-codex"), _bucket("minimax")]
    assert _apply_order(buckets, ["minimax", "deepseek", "openai-codex"]) == [
        "minimax", "deepseek", "openai-codex",
    ]


def test_apply_stored_order_trails_unknown_providers_in_backend_order():
    """Providers missing from the stored order (freshly configured) must
    not vanish: they keep their backend order after the stored ones."""
    buckets = [_bucket("deepseek"), _bucket("openai-codex"), _bucket("minimax")]
    assert _apply_order(buckets, ["minimax"]) == ["minimax", "deepseek", "openai-codex"]


def test_apply_stored_order_ignores_unknown_ids_in_order():
    buckets = [_bucket("deepseek"), _bucket("minimax")]
    assert _apply_order(buckets, ["ghost", "minimax"]) == ["minimax", "deepseek"]


def test_move_provider_id_reorders_and_persists_rest():
    # Default edge is "before": the dragged id lands just ahead of the target.
    assert _move(["a", "b", "c", "d"], "a", "c") == ["b", "a", "c", "d"]
    assert _move(["a", "b", "c", "d"], "d", "b") == ["a", "d", "b", "c"]
    # "after": the dragged id lands just behind the target.
    assert _move(["a", "b", "c", "d"], "a", "c", "after") == ["b", "c", "a", "d"]
    assert _move(["a", "b", "c", "d"], "d", "b", "after") == ["a", "b", "d", "c"]


def test_move_provider_id_is_stable_for_unknown_or_same_targets():
    assert _move(["a", "b", "c"], "a", "a") == ["a", "b", "c"]
    assert _move(["a", "b", "c"], "ghost", "b") == ["a", "b", "c"]
    assert _move(["a", "b", "c"], "a", "ghost") == ["a", "b", "c"]
    # Moving a card "before" its own neighbour is a no-op either way.
    assert _move(["a", "b", "c"], "b", "c") == ["a", "b", "c"]
    assert _move(["a", "b", "c"], "b", "c", "after") == ["a", "c", "b"]


def test_visible_group_preserves_backend_order():
    """Visible buckets must render in the order the backend returned them."""
    buckets = [
        {"id": "deepseek", "label": "DeepSeek", "configured": True, "has_quota": True, "profiles": []},
        {"id": "openai-codex", "label": "Codex", "configured": True, "has_quota": True, "profiles": []},
        {"id": "minimax", "label": "MiniMax", "configured": True, "has_quota": True, "profiles": []},
    ]
    result = _partition(buckets, choices={})
    assert result["visible"] == ["deepseek", "openai-codex", "minimax"]
    assert result["hidden"] == []


def test_hidden_group_is_last_and_preserves_input_order():
    """Hidden buckets (autoHidden OR user-hidden) come last, in input order
    among themselves. The visible group is untouched."""
    buckets = [
        # auto-hidden: configured but no quota and no profiles
        {"id": "copilot", "label": "Copilot", "configured": True, "has_quota": False, "profiles": []},
        {"id": "deepseek", "label": "DeepSeek", "configured": True, "has_quota": True, "profiles": []},
        # user-hidden (explicit choice true)
        {"id": "novita", "label": "Novita", "configured": True, "has_quota": True, "profiles": []},
        {"id": "openai-codex", "label": "Codex", "configured": True, "has_quota": True, "profiles": []},
        # auto-hidden (configured, no quota, no profiles) — must come after
        # the previous auto-hidden one in the input order
        {"id": "anthropic", "label": "Anthropic", "configured": True, "has_quota": False, "profiles": []},
    ]
    result = _partition(buckets, choices={"novita": True})
    # Visible order = backend order with hidden entries stripped out.
    assert result["visible"] == ["deepseek", "openai-codex"]
    # Hidden order = preserved input order among the hidden group.
    assert result["hidden"] == ["copilot", "novita", "anthropic"]


def test_unconfigured_buckets_are_dropped_from_both_groups():
    """Buckets with no quota, no profiles AND no Hermes credentials must
    not enter either group — the partition helper drops them outright.
    """
    buckets = [
        {"id": "deepseek", "label": "DeepSeek", "configured": True, "has_quota": True, "profiles": []},
        # unconfigured: no quota, no profiles, configured=False
        {"id": "ghost", "label": "Ghost", "configured": False, "has_quota": False, "profiles": []},
        {"id": "openai-codex", "label": "Codex", "configured": True, "has_quota": True, "profiles": []},
    ]
    result = _partition(buckets, choices={"ghost": True})
    assert result["visible"] == ["deepseek", "openai-codex"]
    assert result["hidden"] == []
    assert "ghost" not in result["visible"]
    assert "ghost" not in result["hidden"]


def test_unconfigured_bucket_is_dropped_even_when_user_choice_is_set():
    """An explicit user choice on an unconfigured bucket is a no-op: the
    partition helper still drops the bucket because it has nothing to
    render. (The dashboard's own guard above the map call enforces the
    same rule, but the helper should be self-sufficient.)"""
    buckets = [
        {"id": "ghost", "label": "Ghost", "configured": False, "has_quota": False, "profiles": []},
    ]
    result = _partition(buckets, choices={"ghost": True})
    assert result == {"visible": [], "hidden": []}


def test_profile_assigned_buckets_are_never_auto_hidden():
    """A bucket that already carries profiles is useful even when its
    provider card is empty — the partition helper must keep it in the
    visible group unless the user explicitly hid it."""
    buckets = [
        # no quota but has profiles — visible by default
        {"id": "openai-codex", "label": "Codex", "configured": True, "has_quota": False,
         "profiles": [{"profile": "default"}]},
        # no quota, no profiles, configured — auto-hidden
        {"id": "copilot", "label": "Copilot", "configured": True, "has_quota": False, "profiles": []},
    ]
    result = _partition(buckets, choices={})
    assert result["visible"] == ["openai-codex"]
    assert result["hidden"] == ["copilot"]


def test_user_show_choice_overrides_auto_hidden_signal():
    """The dashboard stores ``false`` for an explicit user "show" on an
    auto-hidden bucket. The partition helper's ``isHiddenFn`` is given the
    resolved choice, so passing ``copilot: False`` must pull it back into
    the visible group."""
    buckets = [
        {"id": "copilot", "label": "Copilot", "configured": True, "has_quota": False, "profiles": []},
        {"id": "deepseek", "label": "DeepSeek", "configured": True, "has_quota": True, "profiles": []},
    ]
    result = _partition(buckets, choices={"copilot": False})
    assert result["visible"] == ["copilot", "deepseek"]
    assert result["hidden"] == []


def test_empty_overview_yields_empty_groups():
    assert _partition([], choices={}) == {"visible": [], "hidden": []}


def test_render_visible_then_hidden_concat():
    """Render contract: ``[...visible, ...(customizeMode ? hidden : [])]``.
    When customize mode is off the hidden group must be omitted entirely;
    when on, the hidden group is appended after the visible group."""
    buckets = [
        {"id": "deepseek", "label": "DeepSeek", "configured": True, "has_quota": True, "profiles": []},
        {"id": "openai-codex", "label": "Codex", "configured": True, "has_quota": True, "profiles": []},
        {"id": "copilot", "label": "Copilot", "configured": True, "has_quota": False, "profiles": []},
        {"id": "novita", "label": "Novita", "configured": True, "has_quota": True, "profiles": []},
    ]
    result = _partition(buckets, choices={"novita": True})
    # customize mode = off
    rendered_off = list(result["visible"])  # hidden dropped
    assert rendered_off == ["deepseek", "openai-codex"]
    assert "novita" not in rendered_off
    assert "copilot" not in rendered_off
    # customize mode = on
    rendered_on = list(result["visible"]) + list(result["hidden"])
    assert rendered_on == ["deepseek", "openai-codex", "copilot", "novita"]
    # Visible cards always come before any hidden card.
    assert rendered_on.index("openai-codex") < rendered_on.index("copilot")
    assert rendered_on.index("openai-codex") < rendered_on.index("novita")


def test_partition_is_stable_for_input_order():
    """Swapping the input order must not change each group's relative
    order — partition must preserve backend order inside each group."""
    backend_a = [
        {"id": "deepseek", "label": "DeepSeek", "configured": True, "has_quota": True, "profiles": []},
        {"id": "openai-codex", "label": "Codex", "configured": True, "has_quota": True, "profiles": []},
        {"id": "minimax", "label": "MiniMax", "configured": True, "has_quota": True, "profiles": []},
    ]
    backend_b = list(reversed(backend_a))
    a = _partition(backend_a, choices={})
    b = _partition(backend_b, choices={})
    # The reversed input reverses the visible group too — that's the
    # explicit "preserve input order" rule.
    assert a["visible"] == ["deepseek", "openai-codex", "minimax"]
    assert b["visible"] == ["minimax", "openai-codex", "deepseek"]
    assert a["hidden"] == b["hidden"] == []


def test_auto_hidden_detection_matches_settings_list_rule():
    """The settings dialog lists providers with the same partition rules
    as the main screen: unconfigured buckets are dropped and auto-hidden
    buckets (configured, no quota, no profiles) land in the hidden group.
    The dialog appends the hidden group after visible rows and flags them
    ``autoHidden`` so they render dimmed at the bottom."""
    buckets = [
        {"id": "deepseek", "label": "DeepSeek", "configured": True, "has_quota": True, "profiles": []},
        # auto-hidden: configured but no quota data, no profiles
        {"id": "copilot", "label": "GitHub Copilot", "configured": True, "has_quota": False, "profiles": []},
        # unconfigured: dropped entirely
        {"id": "ghost", "label": "Ghost", "configured": False, "has_quota": False, "profiles": []},
        {"id": "opencode-free", "label": "OpenCode Free", "configured": True, "has_quota": False, "profiles": []},
    ]
    # The settings dialog builds its provider rows exactly like this:
    # partition with the auto-hidden signal, visible first, hidden last.
    payload = json.dumps({"buckets": buckets})
    script = (
        _bundle_loader()
        + "const input = JSON.parse(%r);\n" % payload
        + "const partition = m.exports.partitionBuckets;\n"
        + "const isHidden = function (bucket) {\n"
        + "  const bucketProfiles = Array.isArray(bucket.profiles) ? bucket.profiles : [];\n"
        + "  const hasQuota = Boolean(bucket.has_quota);\n"
        + "  const configured = Boolean(bucket.configured);\n"
        + "  return !hasQuota && !bucketProfiles.length && configured;\n"
        + "};\n"
        + "const p = partition(input.buckets, isHidden);\n"
        + "const rows = p.visible.map(function (b) { return { id: b.id, autoHidden: false }; });\n"
        + "p.hidden.forEach(function (b) { rows.push({ id: b.id, autoHidden: true }); });\n"
        + "process.stdout.write(JSON.stringify(rows));\n"
    )
    rows = json.loads(_node_call(script))
    assert [r["id"] for r in rows] == ["deepseek", "copilot", "opencode-free"]
    assert [r["autoHidden"] for r in rows] == [False, True, True]
    assert not any(r["id"] == "ghost" for r in rows)

