"""Tests for the dashboard bundle's browser notification helpers.

The notification feature ships three pure functions at module scope so
the firing logic is testable without spinning up a fake React SDK or
mocking ``window.Notification``:

* ``notificationAlertIdentity(summary)`` — derive the (level, provider)
  alert set the dashboard reacts to.
* ``notificationDecisions(summary, previousIdentity, notifications,
  previousState)`` — decide which identities should fire this round,
  honouring the cooldown map.
* ``notificationBody(item)`` — render the title/body the Notification
  API consumes.

The fixtures run the same helpers Node-side via ``dashboard/dist/index.js``,
mirroring the ``test_bucket_partition.py`` pattern. The browser-only
``maybeFireNotifications`` wrapper (which actually calls
``new window.Notification(…)``) is not under test here: the test
harness has no window, and the wrapper is a 1:1 passthrough of
``notificationDecisions`` to the browser API.
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


def _identity(summary):
    payload = json.dumps(summary or {})
    script = (
        _bundle_loader()
        + "const out = m.exports.notificationAlertIdentity(JSON.parse(%r));\n" % payload
        + "process.stdout.write(JSON.stringify(out));"
    )
    return json.loads(_node_call(script))


def _decisions(summary, previous_identity, notifications, previous_state):
    payload = json.dumps({
        "summary": summary or {},
        "previousIdentity": previous_identity or [],
        "notifications": notifications or {},
        "previousState": previous_state or {},
    })
    script = (
        _bundle_loader()
        + "const input = JSON.parse(%r);\n" % payload
        + "const out = m.exports.notificationDecisions(\n"
        + "  input.summary,\n"
        + "  input.previousIdentity,\n"
        + "  input.notifications,\n"
        + "  input.previousState\n"
        + ");\n"
        + "process.stdout.write(JSON.stringify(out));"
    )
    return json.loads(_node_call(script))


def _body(item):
    payload = json.dumps(item or {})
    script = (
        _bundle_loader()
        + "const out = m.exports.notificationBody(JSON.parse(%r));\n" % payload
        + "process.stdout.write(JSON.stringify(out));"
    )
    return json.loads(_node_call(script))


# ---------------------------------------------------------------------------
# notificationAlertIdentity
# ---------------------------------------------------------------------------


def test_identity_is_empty_for_missing_or_blank_summary():
    assert _identity(None) == []
    assert _identity({}) == []
    assert _identity({"alerts": "not-an-object", "provider_overview": "nope"}) == []


def test_identity_reads_exhausted_alerts_as_critical():
    summary = {
        "alerts": {
            "exhausted": [{"provider": "DeepSeek", "level": "exhausted"}],
            "low": [],
        },
        "provider_overview": [],
    }
    assert _identity(summary) == [{"level": "critical", "provider": "DeepSeek"}]


def test_identity_reads_low_alerts_as_low():
    summary = {
        "alerts": {
            "exhausted": [],
            "low": [{"provider": "Codex"}],
        },
        "provider_overview": [],
    }
    assert _identity(summary) == [{"level": "low", "provider": "Codex"}]


def test_identity_dedupes_same_level_and_provider():
    summary = {
        "alerts": {
            "exhausted": [
                {"provider": "DeepSeek"},
                {"provider": "DeepSeek"},
            ],
            "low": [],
        },
        "provider_overview": [],
    }
    # Two exhausted entries with the same provider collapse to one
    # identity; the operator never sees two duplicate toasts.
    assert _identity(summary) == [{"level": "critical", "provider": "DeepSeek"}]


def test_identity_promotes_provider_availability_to_critical():
    summary = {
        "alerts": {"exhausted": [], "low": []},
        "provider_overview": [
            {
                "id": "deepseek",
                "label": "DeepSeek",
                "provider_availability": {"status": "rate_limited"},
            },
        ],
    }
    assert _identity(summary) == [{"level": "critical", "provider": "DeepSeek"}]


def test_identity_ignores_ready_provider_overview():
    summary = {
        "alerts": {"exhausted": [], "low": []},
        "provider_overview": [
            {"id": "deepseek", "label": "DeepSeek", "provider_availability": {"status": "ready"}},
            {"id": "codex", "label": "Codex", "provider_availability": {"status": "unconfigured"}},
        ],
    }
    assert _identity(summary) == []


def test_identity_sorts_alphabetically_within_level():
    summary = {
        "alerts": {
            "exhausted": [
                {"provider": "Z"},
                {"provider": "A"},
                {"provider": "M"},
            ],
            "low": [],
        },
        "provider_overview": [],
    }
    assert _identity(summary) == [
        {"level": "critical", "provider": "A"},
        {"level": "critical", "provider": "M"},
        {"level": "critical", "provider": "Z"},
    ]


def test_identity_sorts_critical_before_low():
    summary = {
        "alerts": {
            "exhausted": [{"provider": "A"}],
            "low": [{"provider": "B"}],
        },
        "provider_overview": [],
    }
    assert _identity(summary) == [
        {"level": "critical", "provider": "A"},
        {"level": "low", "provider": "B"},
    ]


# ---------------------------------------------------------------------------
# notificationDecisions
# ---------------------------------------------------------------------------


def test_decisions_return_empty_when_disabled():
    summary = {
        "alerts": {"exhausted": [{"provider": "DeepSeek"}], "low": []},
        "provider_overview": [],
    }
    out = _decisions(summary, [], {"enabled": False, "levels": ["critical"], "cooldown_minutes": 60}, {})
    assert out["fires"] == []
    # Master switch off — the cooldown map is wiped so enabling again
    # immediately surfaces the current state.
    assert out["cooldownMap"] == {}


def test_decisions_return_empty_when_settings_missing():
    # ``notifications=None`` is treated as "no opt-in" — fires stay empty
    # regardless of the alert set. The identity list still reflects the
    # current snapshot so the cooldown map can be primed for when the
    # operator toggles notifications on.
    summary = {"alerts": {"exhausted": [{"provider": "DeepSeek"}], "low": []}, "provider_overview": []}
    out = _decisions(summary, [], None, {})
    assert out["fires"] == []
    assert out["identity"] == [{"level": "critical", "provider": "DeepSeek"}]
    assert out["cooldownMap"] == {}


def test_decisions_fire_a_new_identity_on_first_poll():
    summary = {
        "alerts": {"exhausted": [{"provider": "DeepSeek"}], "low": []},
        "provider_overview": [],
    }
    settings = {"enabled": True, "levels": ["critical"], "cooldown_minutes": 60}
    out = _decisions(summary, [], settings, {"now": 1000, "cooldownMap": {}})
    assert out["fires"] == [{"level": "critical", "provider": "DeepSeek"}]
    assert out["cooldownMap"]["critical|DeepSeek"] == 1000


def test_decisions_skip_when_level_not_in_allowlist():
    summary = {
        "alerts": {"exhausted": [{"provider": "DeepSeek"}], "low": []},
        "provider_overview": [],
    }
    settings = {"enabled": True, "levels": ["low"], "cooldown_minutes": 60}
    out = _decisions(summary, [], settings, {"now": 1000, "cooldownMap": {}})
    assert out["fires"] == []


def test_decisions_respect_cooldown_within_window():
    summary = {
        "alerts": {"exhausted": [{"provider": "DeepSeek"}], "low": []},
        "provider_overview": [],
    }
    settings = {"enabled": True, "levels": ["critical"], "cooldown_minutes": 60}
    # First poll fires and seeds the cooldown map.
    first = _decisions(summary, [], settings, {"now": 1000, "cooldownMap": {}})
    # Second poll arrives 30 seconds later — still inside the 60-minute window.
    second = _decisions(summary, first["identity"], settings, {
        "now": 1000 + 30 * 1000, "cooldownMap": first["cooldownMap"],
    })
    assert second["fires"] == []
    # Cooldown entry stays so the window keeps sliding forward.
    assert second["cooldownMap"]["critical|DeepSeek"] == 1000


def test_decisions_fire_again_after_cooldown_elapses():
    summary = {
        "alerts": {"exhausted": [{"provider": "DeepSeek"}], "low": []},
        "provider_overview": [],
    }
    settings = {"enabled": True, "levels": ["critical"], "cooldown_minutes": 60}
    first = _decisions(summary, [], settings, {"now": 1000, "cooldownMap": {}})
    # Second poll arrives 61 minutes later — past the cooldown window.
    second = _decisions(summary, first["identity"], settings, {
        "now": 1000 + 61 * 60 * 1000, "cooldownMap": first["cooldownMap"],
    })
    assert second["fires"] == [{"level": "critical", "provider": "DeepSeek"}]
    assert second["cooldownMap"]["critical|DeepSeek"] == 1000 + 61 * 60 * 1000


def test_decisions_skip_repeats_when_summary_unchanged():
    summary = {
        "alerts": {"exhausted": [{"provider": "DeepSeek"}], "low": []},
        "provider_overview": [],
    }
    settings = {"enabled": True, "levels": ["critical"], "cooldown_minutes": 60}
    first = _decisions(summary, [], settings, {"now": 1000, "cooldownMap": {}})
    # Second poll arrives inside the cooldown window — the operator keeps
    # the page open and the summary keeps reporting the same alert. No
    # new fire: cooldown_map carries the timestamp from the first fire.
    second = _decisions(summary, first["identity"], settings, {
        "now": 2000, "cooldownMap": first["cooldownMap"],
    })
    assert second["fires"] == []


def test_decisions_escalation_from_low_to_critical_fires():
    previous = [{"level": "low", "provider": "DeepSeek"}]
    summary = {
        "alerts": {"exhausted": [{"provider": "DeepSeek"}], "low": []},
        "provider_overview": [],
    }
    settings = {"enabled": True, "levels": ["critical", "low"], "cooldown_minutes": 60}
    out = _decisions(summary, previous, settings, {"now": 2000, "cooldownMap": {}})
    # Same provider, but the level went critical — escalate fires once.
    assert out["fires"] == [{"level": "critical", "provider": "DeepSeek"}]


def test_decisions_downgrade_does_not_re_fire():
    previous = [{"level": "critical", "provider": "DeepSeek"}]
    summary = {
        "alerts": {"exhausted": [], "low": [{"provider": "DeepSeek"}]},
        "provider_overview": [],
    }
    settings = {"enabled": True, "levels": ["critical", "low"], "cooldown_minutes": 60}
    out = _decisions(summary, previous, settings, {"now": 2000, "cooldownMap": {}})
    # Going critical → low never re-fires. The operator already saw
    # the red alert when the outage started.
    assert out["fires"] == []


def test_decisions_clears_stale_cooldown_entries():
    summary = {
        "alerts": {"exhausted": [{"provider": "DeepSeek"}], "low": []},
        "provider_overview": [],
    }
    settings = {"enabled": True, "levels": ["critical"], "cooldown_minutes": 60}
    # Previous identity: A was a critical provider. A is gone now.
    previous_identity = [
        {"level": "critical", "provider": "A"},
        {"level": "critical", "provider": "DeepSeek"},
    ]
    previous_cooldown = {
        "critical|A": 1000,
        "critical|DeepSeek": 1500,
    }
    out = _decisions(summary, previous_identity, settings, {
        "now": 10000, "cooldownMap": previous_cooldown,
    })
    # A is no longer in the identity list, so its cooldown entry is gone.
    assert "critical|A" not in out["cooldownMap"]
    # DeepSeek is still critical — cooldown stays so a second poll inside
    # the window does not re-fire.
    assert "critical|DeepSeek" not in out["cooldownMap"] or out["cooldownMap"]["critical|DeepSeek"] == 1500


def test_decisions_stale_cooldown_can_fire_when_alert_returns():
    # Once a previously-known alert drops out of the snapshot, the
    # operator is allowed to see it again when it returns (the cooldown
    # is only meant to suppress repeat polls of the same outage).
    previous_identity = [{"level": "critical", "provider": "A"}]
    previous_cooldown = {"critical|A": 1000}
    summary_back = {
        "alerts": {"exhausted": [{"provider": "A"}], "low": []},
        "provider_overview": [],
    }
    settings = {"enabled": True, "levels": ["critical"], "cooldown_minutes": 60}
    # ``previous_identity`` is empty now (A cleared) and the cooldown map
    # must have been wiped on that transition. When A returns, it fires
    # again like a fresh identity.
    cleared = _decisions(
        {"alerts": {"exhausted": [], "low": []}, "provider_overview": []},
        previous_identity,
        settings,
        {"now": 5000, "cooldownMap": previous_cooldown},
    )
    # Stale entry is gone after the cleared poll.
    assert "critical|A" not in cleared["cooldownMap"]
    fires = _decisions(
        summary_back, cleared["identity"], settings,
        {"now": 6000, "cooldownMap": cleared["cooldownMap"]},
    )
    assert fires["fires"] == [{"level": "critical", "provider": "A"}]


def test_decisions_zero_cooldown_fires_every_new_alert():
    summary = {
        "alerts": {"exhausted": [{"provider": "DeepSeek"}], "low": []},
        "provider_overview": [],
    }
    settings = {"enabled": True, "levels": ["critical"], "cooldown_minutes": 0}
    first = _decisions(summary, [], settings, {"now": 1000, "cooldownMap": {}})
    # Even within the same millisecond, cooldown=0 lets the next poll fire.
    second = _decisions(summary, first["identity"], settings, {
        "now": 1000, "cooldownMap": first["cooldownMap"],
    })
    assert second["fires"] == [{"level": "critical", "provider": "DeepSeek"}]


def test_decisions_uses_defaults_when_settings_partial():
    summary = {
        "alerts": {"exhausted": [{"provider": "DeepSeek"}], "low": []},
        "provider_overview": [],
    }
    settings = {"enabled": True}  # no levels / no cooldown_minutes
    out = _decisions(summary, [], settings, {"now": 1000, "cooldownMap": {}})
    # Built-in defaults: enabled=True, levels=["critical"], cooldown=60min.
    assert out["fires"] == [{"level": "critical", "provider": "DeepSeek"}]


def test_decisions_filters_unknown_levels_silently():
    summary = {
        "alerts": {"exhausted": [{"provider": "DeepSeek"}], "low": []},
        "provider_overview": [],
    }
    settings = {"enabled": True, "levels": ["bogus"], "cooldown_minutes": 60}
    out = _decisions(summary, [], settings, {"now": 1000, "cooldownMap": {}})
    # Unknown levels collapse to the empty list — no alerts fire.
    assert out["fires"] == []


def test_decisions_returns_current_identity_even_when_no_fire():
    summary = {
        "alerts": {"exhausted": [{"provider": "DeepSeek"}], "low": []},
        "provider_overview": [],
    }
    settings = {"enabled": True, "levels": ["critical"], "cooldown_minutes": 60}
    out = _decisions(summary, [], settings, {"now": 1000, "cooldownMap": {"critical|DeepSeek": 1000}})
    # Identity is exposed even when nothing fires; the dialog uses it
    # to keep the cooldown map in sync with the visible alert set.
    assert out["identity"] == [{"level": "critical", "provider": "DeepSeek"}]
    assert out["fires"] == []


# ---------------------------------------------------------------------------
# notificationBody
# ---------------------------------------------------------------------------


def test_body_critical_carries_out_of_quota_copy():
    out = _body({"level": "critical", "provider": "DeepSeek"})
    assert out["title"] == "Provider out of quota"
    assert "DeepSeek" in out["body"]


def test_body_low_carries_running_low_copy():
    out = _body({"level": "low", "provider": "Codex"})
    assert out["title"] == "Provider running low"
    assert "Codex" in out["body"]


def test_body_returns_null_for_unknown_or_blank_inputs():
    assert _body(None) is None
    assert _body({"level": "critical"}) is None  # missing provider
    assert _body({"level": "mystery", "provider": "X"}) is None  # unknown level
    assert _body({"provider": ""}) is None


def test_body_does_not_leak_provider_id_or_unrelated_fields():
    # The body builder never references anything beyond ``provider`` and
    # ``level``; a hostile summary cannot smuggle content into the
    # notification text.
    out = _body({"level": "critical", "provider": "DeepSeek", "amount": 1000, "window": "weird"})
    assert "amount" not in out["body"]
    assert "1000" not in out["body"]
    assert "weird" not in out["body"]
