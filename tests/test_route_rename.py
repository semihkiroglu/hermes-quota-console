"""Plugin identity contract.

The plugin id is ``quota-console`` and the dashboard route is
``/quota-console``. The manifest, the frontend register call, the
frontend API constants, and the Hermes config allowlist key must all
agree; any mismatch breaks plugin loading or dashboard routing, so these
are fatal-contract checks rather than style guards.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = REPO_ROOT / "dashboard"
BUNDLE_PATH = DASHBOARD_DIR / "dist" / "index.js"
MANIFEST_PATH = DASHBOARD_DIR / "manifest.json"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_uses_quota_console_name(manifest):
    assert manifest["name"] == "quota-console", (
        "manifest name must be 'quota-console', got %r" % manifest["name"]
    )
    assert manifest["label"] == "Quota Console", (
        "manifest label must stay 'Quota Console', got %r" % manifest["label"]
    )


def test_manifest_route_is_quota_console(manifest):
    tab = manifest.get("tab") or {}
    assert tab.get("path") == "/quota-console", (
        "manifest tab path must be '/quota-console', got %r" % tab.get("path")
    )


def test_frontend_register_uses_quota_console():
    text = BUNDLE_PATH.read_text(encoding="utf-8")
    pattern = re.compile(r'__HERMES_PLUGINS__\.register\(\s*"([^"]+)"\s*,')
    match = pattern.search(text)
    assert match is not None, "bundle does not register a plugin page"
    assert match.group(1) == "quota-console", (
        "frontend must register under 'quota-console', got %r" % match.group(1)
    )


def test_frontend_api_constants_use_quota_console():
    text = BUNDLE_PATH.read_text(encoding="utf-8")
    assert "/api/plugins/quota-console/summary" in text
    assert "/api/plugins/quota-console/reset" in text


def test_runtime_config_key_is_quota_console():
    runtime_path = DASHBOARD_DIR / "runtime.py"
    text = runtime_path.read_text(encoding="utf-8")
    assert "plugins.get(\"quota-console\")" in text, (
        "runtime.py must look up the allowlist under plugins.quota-console"
    )
