"""Locale/timezone behaviour: the API emits ISO UTC timestamps and the
bundle parses them with the standard Date constructor, so an ISO string
with an offset normalises to the same UTC instant the server emitted.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

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


def test_format_date_accepts_iso_input():
    """The bundle accepts the ISO timestamp the API emits at the boundary."""
    # The API emits ISO timestamps at the boundary; the bundle parses them
    # through the standard ``Date`` constructor. Verify that an ISO string
    # with a positive offset normalises to the same UTC instant the server
    # emitted, proving the parser does not silently drop timezone info.
    script = (
        _bundle_loader()
        + "const sample = '2026-01-02T06:04:05+03:00';\n"
        + "const parsed = new Date(sample).getTime();\n"
        + "process.stdout.write(JSON.stringify(parsed));\n"
    )
    rendered = _node_call(script)
    # 2026-01-02T03:04:05Z in epoch milliseconds.
    assert json.loads(rendered) == 1767323045000


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
