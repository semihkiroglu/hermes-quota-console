# Contribution Guide (CONTRIBUTING)

This document is for anyone who wants to contribute to the hermes-quota-console
repository: process, standards, and boundaries. It explains **how** to
contribute: process (1-5), adding a provider ([ADDING_A_PROVIDER.md](docs/ADDING_A_PROVIDER.md)),
writing tests ([WRITING_TESTS.md](docs/WRITING_TESTS.md)), documentation (6),
document map (7), and deploying to live (8). AI agents are additionally bound
by the working contract in [AGENTS.md](AGENTS.md).

## 1. Working model

- The repo is **public** with two protected lines:
  - **`unstable`** (default branch) — the contribution line. Community
    contributions open PRs here (the repository default makes this the
    natural target); after the gate (section 2) passes and a maintainer
    approves, the PR merges.
  - **`stable`** — the release line, maintainer-only. Changes reach it only
    through the maintainer's promote step; a PR opened against `stable` by
    anyone else is closed automatically and pointed at `unstable`.
- The live Hermes plugin checkout lives in a separate directory
  (`~/.hermes/plugins/quota-console`) and is a copy of `stable`; pull there
  only when a release lands.
- Public repo: never push any file containing secrets or personal
  configuration; every push is visible to everyone.

## 2. Pre-PR gate (mandatory)

Before opening a PR, run **all four** on every change:

```bash
uv run --extra dev pytest -q --tb=short   # all tests green
node --check dashboard/dist/index.js      # UI bundle syntax
python3 -m compileall -q dashboard tests  # Python compilation
python3 tools/check_secret_safety.py      # secret leak scan
```

If any gate is red the work is **not done**; fix and re-run.

## 3. Environment

```bash
uv sync --extra dev          # dependencies (pytest, pytest-cov)
uv run pytest -q --tb=short  # test suite
```

Python ≥ 3.10. A Hermes install is **not required** for tests — tests run
Hermes-free via isolated imports (see [WRITING_TESTS.md](docs/WRITING_TESTS.md)).

## 4. Change types and scope discipline

| Type | Example | Rule |
|---|---|---|
| New built-in provider | `dashboard/providers/nous.py` | Follow [ADDING_A_PROVIDER.md](docs/ADDING_A_PROVIDER.md); adapter + test in one change. |
| Backend behavior | alert computation, settings schema | `settings.py` schema change = compatibility with old disk files (unknown key rejection/drop) + test. |
| UI | `dashboard/dist/index.js` / `style.css` | Only the `h()` SDK pattern; no separate framework copy. If bundle behavior needs testing, use a module-scope export + Node test. |
| Documentation | `README.md`, `CONTRIBUTING.md`, `docs/` | Markdown English; code samples and commands stay English. |

Boundaries:

- **Never touch the Hermes core.** If a change on the Hermes side is needed,
  it is not solved in this repo; access is only through the fail-closed
  adapter functions in `runtime.py`.
- **Never leak credentials/endpoints/mappings.** Credential values, endpoint
  URLs, raw provider responses, and exception text are never written to logs,
  API responses, summaries, or the UI; the UI only renders normalized
  snapshots, settings store only env **names**.
- **Never assume currency/limit/plan names.** If the API does not return it,
  the field stays empty/null; no hardcoded symbols or thresholds (see
  [ADDING_A_PROVIDER.md](docs/ADDING_A_PROVIDER.md) §3).
- **Never show unverified data.** No invented balance/plan; quota/balance is
  only shown for verified adapters (see
  [ADDING_A_PROVIDER.md](docs/ADDING_A_PROVIDER.md) §1).

## 5. Commit and message standards

- Small, single-purpose commits: `fix:`, `feat:`, `docs:`, `test:`,
  `refactor:` prefix + short description (English).
- Examples: `feat: add nous portal quota adapter`, `fix: settings dialog
  follows main-screen visibility rules`, `docs: explain how to add a
  provider`.
- A commit never mixes a behavior change with an unrelated doc change.

### 5.1 Versioning and releases

Versioning is **manual**: when the maintainer promotes to `stable`, the
version in `pyproject.toml` is bumped — patch (`0.1.0` → `0.1.1`) for fixes,
minor (`0.1.0` → `0.2.0`) for features, major (`0.1.0` → `1.0.0`) for
breaking changes. When the change lands on `stable`, the Tag and release
workflow reads the new version, creates the `v<version>` tag, and publishes
a GitHub release with auto-generated notes (idempotent — a tag/release that
already exists is never moved or re-created). `unstable` carries no versions.

## 6. Documentation

- All docs and in-code comments are English.
- Docs are updated together with code changes; they never go stale.

## 7. Document map

| File | Content |
|---|---|
| `AGENTS.md` | AI agent contract |
| `CONTRIBUTING.md` | This file: contribution guide |
| `README.md` | User documentation |
| `SECURITY.md` | Security notice |
| `SUPPORT.md` | Support channels (plugin vs. upstream Hermes Agent) |
| `docs/ADDING_A_PROVIDER.md` | Step-by-step provider adapter guide |
| `docs/WRITING_TESTS.md` | Test-writing guide and patterns |
| `docs/demo.gif` | Dashboard demo recording |

Community-health files under `.github/`: issue templates (bug report,
provider/feature request), PR template, dependabot config.

## 8. Deploying to live (only after a release)

```bash
# After a release lands on stable:
cd ~/.hermes/plugins/quota-console && git pull --ff-only origin stable
# If only static files changed (dashboard/dist/*) no restart is needed:
# the Hermes web server reads plugin assets from disk on every request.
# If the Python side changed (plugin_api.py, providers/ etc.) the Hermes
# gateway process is restarted (no systemd unit; the process runs directly).
```

The live checkout is a copy of `stable`; everything released to stable also
goes live. No PR without a green gate, no promote without a release.
