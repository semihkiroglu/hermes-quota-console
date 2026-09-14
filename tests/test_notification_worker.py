"""Fixtures for the notification service worker.

Android Chrome refuses to construct ``Notification`` objects from a page
("Illegal constructor — use ServiceWorkerRegistration.showNotification"), so
the bundle routes every notification through ``dist/sw.js``. These fixtures
load that worker inside a fake service-worker global and assert what a click
does, without a browser or the network.
"""

import json
import pathlib
import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient

WORKER = pathlib.Path(__file__).resolve().parents[1] / "dashboard" / "dist" / "sw.js"

_TEMPLATE = """
const listeners = {};
const state = { closed: false, focused: false, opened: false };
global.state = state;
global.self = {
  addEventListener: function (name, fn) { listeners[name] = fn; },
  clients: {
    matchAll: function () { return Promise.resolve(CLIENTS_JS); },
    openWindow: function () { state.opened = true; return Promise.resolve(null); },
  },
};
require(WORKER_PATH);
const event = {
  notification: { close: function () { state.closed = true; } },
  waitUntil: function (promise) { global.pending = promise; },
};
listeners["notificationclick"](event);
global.pending.then(function () { process.stdout.write(JSON.stringify(state)); });
"""

_CLIENT = "[{ focus: function () { global.state.focused = true; return Promise.resolve(null); } }]"


def _run_click_handler(clients_js):
    script = (
        _TEMPLATE.replace("CLIENTS_JS", clients_js)
        .replace("WORKER_PATH", json.dumps(str(WORKER)))
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_click_closes_the_notification_and_focuses_an_open_tab():
    state = _run_click_handler(_CLIENT)
    assert state["closed"] is True
    assert state["focused"] is True
    assert state["opened"] is False


def test_click_opens_the_dashboard_when_no_tab_is_open():
    state = _run_click_handler("[]")
    assert state["closed"] is True
    assert state["focused"] is False
    assert state["opened"] is True


def test_worker_route_serves_the_script(plugin_api):
    """The worker ships from its own route.

    The dashboard only serves the assets its manifest names, so the worker
    needs an endpoint of its own — a 404 here would leave Android phones with
    no working notification path at all.
    """
    app = FastAPI()
    app.include_router(plugin_api.router)
    response = TestClient(app).get("/sw.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "notificationclick" in response.text
