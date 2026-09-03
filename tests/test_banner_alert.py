"""Tests for the dashboard bundle's shell-banner aggregation helper.

The plugin registers a component into the shell's ``header-banner`` slot
so quota alerts appear as a full-width strip on every dashboard page.
The bundle exposes ``bannerAlertFromSummary`` at module scope so we can
exercise the same aggregation the browser renders with, without spinning
up a fake React SDK.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATH = REPO_ROOT / "dashboard" / "dist" / "index.js"


def _banner(summary_payload):
    """Run bannerAlertFromSummary(payload) under Node."""
    payload = json.dumps(summary_payload)
    script = (
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
        "const result = m.exports.bannerAlertFromSummary(%s);\n"
        "process.stdout.write(JSON.stringify(result));\n" % (str(BUNDLE_PATH), payload)
    )
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
    return json.loads(completed.stdout)


def _summary(low=None, exhausted=None):
    return {"alerts": {"low": low or [], "exhausted": exhausted or []}}


def test_no_alerts_returns_none():
    assert _banner(_summary()) is None
    assert _banner(None) is None
    assert _banner({}) is None
    assert _banner({"alerts": None}) is None


def test_low_alert_builds_yellow_state():
    result = _banner(_summary(low=[{"provider": "deepseek"}, {"provider": "minimax"}]))
    assert result == {
        "level": "low",
        "count": 2,
        "names": ["deepseek", "minimax"],
        "extra": 0,
    }


def test_exhausted_wins_over_low():
    result = _banner(
        _summary(
            low=[{"provider": "deepseek"}],
            exhausted=[{"provider": "openai-codex"}],
        )
    )
    assert result["level"] == "exhausted"
    assert result["count"] == 1
    assert result["names"] == ["openai-codex"]


def test_more_than_three_names_reports_extra():
    items = [{"provider": "p%d" % i} for i in range(5)]
    result = _banner(_summary(low=items))
    assert result["names"] == ["p0", "p1", "p2"]
    assert result["extra"] == 2


def test_missing_provider_label_falls_back_to_unknown():
    result = _banner(_summary(exhausted=[{"label": "no provider key"}]))
    assert result["names"] == ["unknown"]
