# Writing Tests

How to test hermes-quota-console. Core rule: **tests run without a Hermes
install, network, or credentials.** Run the suite with
`uv run --extra dev pytest -q --tb=short`. Process and the pre-PR gate live
in [CONTRIBUTING.md](../CONTRIBUTING.md); the provider guide is in
[ADDING_A_PROVIDER.md](ADDING_A_PROVIDER.md).

## 1. Pure unit tests (helpers, alert functions)

Direct import + pure function call. No Hermes, no filesystem.

Example: `tests/test_provider_helpers.py`

```python
from dashboard.providers.base import finite_number, iso_time, percent

def test_provider_helpers_reject_non_finite_values():
    assert finite_number("3.5") == 3.5
    assert finite_number(float("nan")) is None
    assert percent(120) == 100
```

The alert layer works the same way (`tests/test_alert_layer.py`):
`level_for_window`, `level_for_balance`, `bucket_alert` are called directly —
scenarios pinned here include: `ok` when the threshold is `None`, fallback
never raising an alert on its own, and fallback only being considered when the
primary is exhausted.

**When:** the module contains pure logic. Provider adapters also fall into
this category — `ProviderContext` is constructed by hand:

```python
def _context(**overrides):
    base = {
        "runtime_credentials": lambda provider_id: {"api_key": "test-key"},
        "get_json": lambda *a, **k: {"balance_infos": [...]},
        "base_card": base_card,          # from dashboard.providers.base
        "unavailable": unavailable,      # from dashboard.providers.base
        "log_unavailable": lambda provider_id: None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)

def test_fetch_ok():
    card = deepseek.fetch(_context())
    assert card["status"] == "ok"
    assert card["balances"][0]["currency"] == "USD"
```

HTTP-error scenario: swap `get_json` for a lambda that `raise RuntimeError`s;
the result must be an `unavailable` card and the exception must not propagate.
(Patterns: `tests/test_provider_fixtures.py`, `tests/test_plan_fallback.py`.)

## 2. Isolated module loading (plugin_api, settings)

`plugin_api.py` and `settings.py` are tested Hermes-free: the module is loaded
from file with `importlib` under a **unique name**, placed in `sys.modules`,
and cleaned up after the test. This keeps monkeypatches from leaking into
other tests.

Ready-made fixtures live in `tests/conftest.py`:

- `plugin_api` — loads `plugin_api.py` in isolation (for API endpoint tests).
- `load_fixture` — reads JSON fixtures under `tests/fixtures/`.

Example (settings layer, the `isolated_settings` fixture in
`tests/test_settings_storage.py`):

```python
@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    settings = _load_settings()          # uuid-suffixed isolated import
    target = tmp_path / "config.json"
    monkeypatch.setattr(settings, "_storage_dir", lambda: tmp_path)
    monkeypatch.setattr(settings, "storage_path", lambda: target)
    return SimpleNamespace(module=settings, path=target)
```

**When:** modules that write to disk (`settings.py`), touch Hermes state
(`plugin_api.py`), or need isolation. `sys.modules` cleanup is mandatory —
otherwise the "re-import the same file and see the monkeypatch" pattern
breaks.

## 3. Registry tests (monkeypatch)

`registry.py` connects to Hermes via `runtime._load_allowlist()` and
`metadata.entry_points`; both are monkeypatched. Entry-point discovery is
faked with a function:

```python
def _entry_points(entries):
    group = registry_mod._ENTRY_POINT_GROUP

    def fake(group=None):
        if group != registry_mod._ENTRY_POINT_GROUP:
            return []
        return [metadata.EntryPoint(name=name, value=value, group=group) for name, value in entries]

    return fake
```

Scenarios covered (`tests/test_registry.py`): built-in scope when no allowlist
exists; allowlist filtering; empty allowlist disables everything; ID
normalization; external entry-points only load with an allowlist entry; broken
entry-point fails closed (import error, wrong type, ID mismatch); the same ID
is only registered once.

Catalog-merge tests live in `tests/test_dynamic_registry.py` —
`runtime.list_catalog_providers` is monkeypatched (no Hermes catalog in the
test environment).

## 4. UI bundle tests (Node subprocess)

`dashboard/dist/index.js` is a browser bundle; it cannot be imported from
Python. The bundle's **pure functions exported at module scope** are called
from Node. Pattern in `tests/test_bucket_partition.py`:

```python
def _bundle_loader() -> str:
    return (
        "const fs = require('fs');\n"
        "const path = %r;\n"                    # bundle file path
        "const code = fs.readFileSync(path, 'utf8');\n"
        "const window = { __HERMES_PLUGIN_SDK__: null, __HERMES_PLUGINS__: { register: function () {} } };\n"
        "const fn = new Function('module', 'window', code);\n"
        "const m = { exports: {} };\n"
        "fn(m, window);\n"
    ) % str(BUNDLE_PATH)

def _node_call(script: str) -> str:
    completed = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout
```

**Rule:** when a bundle change needs testing, put the behavior in a
module-scope function (e.g. `partitionBuckets`, `projectProfiles`,
`canResetProfileStatus`) and export it; the test calls that function from
Node. There are no direct DOM/React render tests — UI logic is moved into pure
functions and tested there.

Bundle-specific string guards also live here: tests asserting a class/text/flag
exists in the bundle via `BUNDLE_PATH.read_text()` (e.g. the settings dialog's
auto-hidden marker).

## 5. Secret-safety tests

A separate concern class, separate files: `tests/test_secret_safety.py` (tool
scan), `tests/test_reset_preservation.py` (reset never touches credentials),
and the never-leak cases inside `tests/test_settings_storage.py` (secret-shaped
payloads are rejected and never written to disk files or responses).

`python3 tools/check_secret_safety.py` also runs on every change — it scans
the source tree for secret patterns (`sk-...`, `gh...`, JWT, bearer, etc.) and
forbidden filenames (`.env`, `auth.json`, `config.yaml`...). If a new test
fixture contains real credentials, use non-pattern dummy values.

## 6. Which file for what?

| Change | Test file | Pattern |
|---|---|---|
| `providers/base.py` helpers/alert layer | `test_provider_helpers.py`, `test_alert_layer.py` | pure unit |
| New provider adapter | `tests/test_<id>.py` (new) | fake context |
| `providers/registry.py` | `test_registry.py`, `test_dynamic_registry.py` | monkeypatch |
| `settings.py` | `test_settings_storage.py` | isolated module + tmp_path |
| `plugin_api.py` endpoints | `test_settings_endpoints.py`, `test_provider_overview.py`, `test_plugin_version.py` | `plugin_api` fixture |
| `dist/index.js` logic | `test_bucket_partition.py`, `test_banner_alert.py` | Node + module export |
| Secret/reset behavior | `test_secret_safety.py`, `test_reset_preservation.py` | scan + isolated module |

## 7. What a good test looks like

1. **Hermes-free and network-free** — no test connects to a live Hermes
   directory, real credentials, or the internet.
2. **Pins behavior, not implementation** — e.g. test the behavior "fallback
   alone never raises an alert", not internal variable names.
3. **Edge cases come from real risks**: zero balance, missing plan,
   `nan`/`inf`, broken JSON, secret-shaped input, unknown settings key,
   `None` threshold (alerts off).
4. **Red→green**: test the behavior first (red), then write the code (green).
   A behavior change without a test is not complete.
5. No absence tests ("file X does not exist") — test behavior. (Bundle guards
   are the only exception: since we cannot run in the browser, we assert
   string presence.)

Test requirement summary: every code change that alters behavior ships with a
test (red→green). No test, no completed change. For UI bundle changes prefer
pure functions callable from Node.
