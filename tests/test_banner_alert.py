"""Tests for the dashboard bundle's shell-banner aggregation helper.

The plugin registers a component into the shell's ``header-banner`` slot
so provider problems appear as a full-width strip on every dashboard
page. The bundle exposes ``bannerAlertFromSummary`` at module scope so we
can exercise the same aggregation the browser renders with, without
spinning up a fake React SDK.

Banner visibility contract:
- Critical (red): exhausted quota (summary.alerts.exhausted) or a
  provider whose profiles are rate_limited/degraded (reset action) or
  auth_failed (credential problem).
- Low (yellow): only the ``summary.alerts.low`` family.
- Unavailable snapshots never reach the banner: transient fetch
  failures already show on the card and the next poll usually clears
  them, so a banner would only add noise.
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


def _summary(low=None, exhausted=None, overview=None):
    payload = {"alerts": {"low": low or [], "exhausted": exhausted or []}}
    if overview is not None:
        payload["provider_overview"] = overview
    return payload


def _bucket(provider_id, label, status):
    return {
        "id": provider_id,
        "label": label,
        "provider_availability": {"status": status, "status_label": status},
    }


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


def test_exhausted_quota_is_critical_and_wins_over_low():
    result = _banner(
        _summary(
            low=[{"provider": "deepseek"}],
            exhausted=[{"provider": "openai-codex"}],
        )
    )
    assert result["level"] == "critical"
    assert result["count"] == 1
    assert result["names"] == ["openai-codex"]


def test_rate_limited_provider_is_critical():
    result = _banner(
        _summary(
            overview=[_bucket("openai-codex", "OpenAI Codex", "rate_limited")]
        )
    )
    assert result["level"] == "critical"
    assert result["names"] == ["OpenAI Codex"]


def test_degraded_provider_is_critical():
    result = _banner(
        _summary(overview=[_bucket("minimax", "MiniMax", "degraded")])
    )
    assert result["level"] == "critical"
    assert result["names"] == ["MiniMax"]


def test_auth_failed_provider_is_critical():
    result = _banner(
        _summary(overview=[_bucket("deepseek", "DeepSeek", "auth_failed")])
    )
    assert result["level"] == "critical"
    assert result["names"] == ["DeepSeek"]


def test_unavailable_provider_never_reaches_banner():
    # A transient fetch failure with no profiles must stay off the banner;
    # it already shows on the card and the next poll usually clears it.
    result = _banner(
        _summary(
            overview=[_bucket("openai-codex", "OpenAI Codex", "unavailable")]
        )
    )
    assert result is None


def test_unavailable_with_other_critical_state_still_attends():
    # Unavailable alone is ignored, but a second provider that is
    # genuinely rate-limited still raises the banner.
    result = _banner(
        _summary(
            overview=[
                _bucket("openai-codex", "OpenAI Codex", "unavailable"),
                _bucket("deepseek", "DeepSeek", "auth_failed"),
            ]
        )
    )
    assert result["level"] == "critical"
    assert result["names"] == ["DeepSeek"]


def test_status_and_quota_labels_deduplicate():
    # Same provider appears both as exhausted quota and as a
    # rate_limited bucket: one label, one count.
    result = _banner(
        _summary(
            exhausted=[{"provider": "DeepSeek"}],
            overview=[_bucket("deepseek", "DeepSeek", "rate_limited")],
        )
    )
    assert result["level"] == "critical"
    assert result["count"] == 1
    assert result["names"] == ["DeepSeek"]


def test_ready_provider_stays_off_banner():
    result = _banner(
        _summary(
            overview=[_bucket("minimax", "MiniMax", "ready")]
        )
    )
    assert result is None


def test_more_than_three_names_reports_extra():
    items = [{"provider": "p%d" % i} for i in range(5)]
    result = _banner(_summary(low=items))
    assert result["names"] == ["p0", "p1", "p2"]
    assert result["extra"] == 2


def _release_update(current, latest):
    """Run releaseUpdate(current, latest) under Node."""
    payload = json.dumps([current, latest])
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
        "const result = m.exports.releaseUpdate.apply(null, %s);\n"
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


def test_release_update_returns_newer_tag():
    assert _release_update("0.1.1", "v0.1.2") == "v0.1.2"
    assert _release_update("v0.1.1", "v0.1.2") == "v0.1.2"


def test_release_update_null_when_same_normalised():
    assert _release_update("0.1.1", "v0.1.1") is None
    assert _release_update("v0.1.1", "0.1.1") is None


def test_release_update_null_when_missing():
    assert _release_update(None, "v0.1.2") is None
    assert _release_update("0.1.1", None) is None
    assert _release_update(None, None) is None


def test_release_update_null_when_latest_is_older():
    # A dev checkout ahead of the newest published tag must NOT be
    # reported as an update (this was the bug: plain inequality check).
    assert _release_update("0.1.3", "v0.1.2") is None
    assert _release_update("0.2.0", "v0.1.9") is None


def test_release_update_uses_numeric_version_order():
    # String comparison would treat "1.10.0" < "1.9.0"; semver must not.
    assert _release_update("1.9.0", "v1.10.0") == "v1.10.0"
    assert _release_update("1.10.0", "v1.9.0") is None


def test_release_update_fail_closed_on_garbage():
    assert _release_update("not-a-version", "v0.1.2") is None
    assert _release_update("0.1.1", "newest") is None
