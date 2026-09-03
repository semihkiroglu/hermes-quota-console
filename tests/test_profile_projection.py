"""Tests for the dashboard bundle's profile projection rules.

Arbitrary profile names (including two non-worker profiles) project
correctly. The bundle exposes ``projectProfiles`` at module scope so we
can exercise the same function the browser renders with, without
spinning up a fake React SDK.
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


def _project(profiles):
    payload = json.dumps(profiles)
    script = (
        _bundle_loader()
        + "const profiles = JSON.parse(%r);\n" % payload
        + "const result = m.exports.projectProfiles(profiles);\n"
        + "process.stdout.write(JSON.stringify(result));\n"
    )
    return json.loads(_node_call(script))


def _can_reset(status):
    payload = json.dumps(status)
    script = (
        _bundle_loader()
        + "const status = JSON.parse(%r);\n" % payload
        + "process.stdout.write(JSON.stringify(m.exports.canResetProfileStatus(status)));\n"
    )
    return json.loads(_node_call(script))


def test_reset_helper_only_enables_actionable_profile_states():
    # Reset lifts a Hermes-imposed usage block (rate limit / degraded
    # state). auth_failed is a credential problem, not a reset concern.
    for status in ("ready", "unconfigured", "untracked", "auth_failed", None, ""):
        assert _can_reset(status) is False
    for status in ("rate_limited", "degraded"):
        assert _can_reset(status) is True


def test_default_profile_is_separated_from_others():
    profiles = [
        {"id": "default", "profile": "default", "model": "gpt-5.6-luna", "provider": "openai-codex"},
        {"id": "researcher-a", "profile": "researcher-a", "model": "deepseek-v4-flash", "provider": "deepseek"},
        {"id": "writer", "profile": "writer", "model": "gpt-5.6-luna", "provider": "openai-codex"},
    ]
    result = _project(profiles)
    assert result["defaultProfile"]["profile"] == "default"
    others = [p["profile"] for p in result["otherProfiles"]]
    assert others == ["researcher-a", "writer"]


def test_non_worker_names_render_with_two_workers():
    """Two non-worker names must render alongside workers."""
    profiles = [
        {"id": "worker-3", "profile": "worker-3", "model": "MiniMax-M3", "provider": "minimax"},
        {"id": "researcher-a", "profile": "researcher-a", "model": "deepseek-v4-flash", "provider": "deepseek"},
        {"id": "writer", "profile": "writer", "model": "gpt-5.6-luna", "provider": "openai-codex"},
        {"id": "default", "profile": "default", "model": "gpt-5.6-luna", "provider": "openai-codex"},
        {"id": "worker-1", "profile": "worker-1", "model": "deepseek-v4-flash", "provider": "deepseek"},
        {"id": "ops-bot", "profile": "ops-bot", "model": "deepseek-v4-pro", "provider": "deepseek"},
    ]
    result = _project(profiles)
    others = [p["profile"] for p in result["otherProfiles"]]
    assert others == ["ops-bot", "researcher-a", "worker-1", "worker-3", "writer"]
    # Two non-worker names are present and rendered.
    assert "researcher-a" in others
    assert "writer" in others
    assert "ops-bot" in others


def test_backend_order_does_not_change_visual_order():
    """Backend iteration order must not leak into the UI."""
    backend_order = ["worker-3", "researcher-a", "writer", "worker-1", "ops-bot", "default"]
    profiles = [
        {"id": name, "profile": name, "model": "m", "provider": "p"} for name in backend_order
    ]
    others_one = [p["profile"] for p in _project(profiles)["otherProfiles"]]
    reversed_payload = list(reversed(profiles))
    others_two = [p["profile"] for p in _project(reversed_payload)["otherProfiles"]]
    assert others_one == others_two
    assert others_one == sorted(others_one)


def test_no_default_profile_yields_null_solo_row():
    profiles = [
        {"id": "researcher-a", "profile": "researcher-a", "model": "x", "provider": "y"},
        {"id": "writer", "profile": "writer", "model": "x", "provider": "y"},
    ]
    result = _project(profiles)
    assert result["defaultProfile"] is None
    assert [p["profile"] for p in result["otherProfiles"]] == ["researcher-a", "writer"]


def test_empty_payload_is_safe():
    result = _project([])
    assert result == {"defaultProfile": None, "otherProfiles": []}

