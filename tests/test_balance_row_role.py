"""Tests for the dashboard bundle's balance-row className helper.

The alarm layer (see dashboard/providers/base.py) tags every balance item
with ``role`` ("primary" or "fallback") and ``level`` ("ok" | "low" |
"exhausted" | "unknown"). A fallback item never raises an alert per
AGENTS rule 5 — its computed ``level`` is informational only — so the
browser's BalanceRow must render fallback balances in the neutral style
regardless of level. Primary balances keep their level-driven colouring
(DeepSeek's balance-only card is balance-primary by design).

The bundle exposes ``balanceRowClass(role, level)`` at module scope so we
can exercise the same decision the browser renders with, without spinning
up a fake React SDK.
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


def _class_name(role, level):
    """Run balanceRowClass(role, level) under Node.

    Returns the className suffix the row would render with. The browser's
    BalanceRow wraps it as ``"usages-balance" + (suffix ? " " + suffix : "")``;
    we only assert on the suffix here so the contract is unambiguous.
    """
    payload = json.dumps({"role": role, "level": level})
    script = (
        _bundle_loader()
        + "const input = JSON.parse(%r);\n" % payload
        + "const out = m.exports.balanceRowClass(input.role, input.level);\n"
        + "process.stdout.write(JSON.stringify(out));\n"
    )
    return json.loads(_node_call(script))


def test_primary_balance_keeps_level_driven_class():
    """Primary balances (e.g. DeepSeek's wallet) must keep their coloured
    tint when the alert layer reports low/exhausted/unknown — they ARE
    the alarm source for balance-only providers."""
    assert _class_name("primary", "low") == "usages-level--low"
    assert _class_name("primary", "exhausted") == "usages-level--exhausted"
    assert _class_name("primary", "unknown") == "usages-level--unknown"


def test_primary_balance_with_ok_level_renders_neutral():
    """A primary balance at level "ok" must render the neutral class so
    it visually agrees with the bucket's green status."""
    assert _class_name("primary", "ok") == ""


def test_fallback_balance_never_tints_regardless_of_level():
    """Fallback balances (the wallet behind a still-healthy plan) carry
    informational level only. AGENTS rule 5 forbids surfacing them as
    alarms — every fallback level must map to the neutral class."""
    assert _class_name("fallback", "low") == ""
    assert _class_name("fallback", "exhausted") == ""
    assert _class_name("fallback", "unknown") == ""
    assert _class_name("fallback", "ok") == ""


def test_unknown_role_falls_back_to_level_driven_class():
    """An item without a role attribute (defensive default) must keep
    the existing level-driven behaviour rather than silently neutralising
    the row — that keeps the visual signal intact until the backend
    annotation step is verified."""
    assert _class_name(None, "low") == "usages-level--low"
    assert _class_name("", "exhausted") == "usages-level--exhausted"
    assert _class_name(None, "ok") == ""


def test_live_minimax_card_scenario():
    """Reproduce the live evidence from the operator report: a primary
    bucket with healthy windows but a fallback balance at amount=0 /
    level=exhausted. The helper must return the neutral suffix so the
    row drops the destructive tint even though level="exhausted"."""
    # The live card:
    #   windows: ok, ok      -> primary, bucket level = ok
    #   balances: { amount: 0, role: "fallback", level: "exhausted" }
    assert _class_name("fallback", "exhausted") == ""
    # And the matching primary balance (e.g. DeepSeek) keeps the alarm.
    assert _class_name("primary", "exhausted") == "usages-level--exhausted"
