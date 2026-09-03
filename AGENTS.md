# AGENTS.md

This file is **exclusively for AI agents**: a short working contract for
agents making code changes in this repository — project definition,
architecture map, immutable rules, and the gate. Read it before changing
code; architectural decisions and security boundaries are not open for
discussion. Process and contribution boundaries live in
[CONTRIBUTING.md](CONTRIBUTING.md) — agents must follow those rules too.

## What the project is

A **provider quota/balance display and alert plugin** for the Hermes Agent
dashboard (`quota-console`). It does not fork Hermes or touch its source; it
uses Hermes' dashboard extension contract (`manifest.json` + `dist/index.js`
bundle + `plugin_api.py` FastAPI router).

- Dashboard route: `/quota-console`, API base: `/api/plugins/quota-console/*`
- Working model: **providers are added in code**.
- Two layers: (1) every Hermes provider appears as an identity/profile row,
  (2) only verified sources (built-in adapters) show real quota/balance.
  **Fake balances are forbidden** — no fetch for unverified endpoints.

## Architecture map

| File | Role |
|---|---|
| `dashboard/plugin_api.py` | FastAPI router: summary, reset, settings GET/PUT. Single access point to Hermes through `runtime.py`. |
| `dashboard/runtime.py` | **Hermes compatibility boundary.** The only module importing Hermes packages directly. `hermes_cli`/`agent` imports are collected here; provider modules and the API never import Hermes directly. |
| `dashboard/settings.py` | Operator settings store (two layers: `defaults` + per-provider `providers`). Disk: `<hermes-home>/plugin-data/quota-console/config.json`. |
| `dashboard/providers/base.py` | `ProviderSpec`, `ProviderContext`, normalization helpers (`finite_number`, `percent`, `iso_time`), `base_card`, `unavailable`, alert layer (`level_for_window`, `level_for_balance`, `annotate_items`, `bucket_alert`). |
| `dashboard/providers/registry.py` | Registry junction: built-in SPECs + Hermes catalog merge + entry-point allowlist. |
| `dashboard/providers/deepseek.py` | Example built-in adapter (simple, HTTP GET, balances). |
| `dashboard/providers/minimax.py` | Example built-in adapter (complex, windows + balances). |
| `dashboard/providers/openai_codex.py` | Example built-in adapter (uses Hermes account-usage helper). |
| `dashboard/dist/index.js` | Prebuilt dashboard UI bundle (single file, SDK `h()`). |
| `dashboard/dist/style.css` | UI styles. |
| `tests/` | pytest suite + Node bundle tests (`test_bucket_partition.py`). |
| `tools/check_secret_safety.py` | Secret/credential leak gate. |

## Immutable rules

1. **Never write to the Hermes core.** Access only through the fail-closed
   adapter functions in `dashboard/runtime.py`.
2. **Credential values, endpoint URLs, mappings, raw provider responses, and
   exception text never** reach logs, API responses, summaries, or the
   browser. The UI only receives normalized, browser-safe snapshots. The
   settings API stores only env **names** as references.
3. **Never assume currency/limits.** If the API returns no currency, it stays
   `null`; no hardcoded `$`/`TRY`/thresholds. Alert thresholds default to
   `null` (off); the operator enables them per provider.
4. **Never show unverified data.** No invented balance/plan on `profile-only`
   provider cards. Plan names are only populated when the API provides them.
5. **Alert roles:** `windows[]`→primary, `balances[]`→fallback (balance-only
   providers use balance as primary). Only the primary determines bucket
   level; fallback alone never raises an alert.
6. **A built-in adapter overrides an external copy under its own ID; without
   an allowlist entry the external adapter is not loaded.**
7. **Language:** repository content — code, UI, logs, commit messages, and
   docs — is English.
8. Code changes ship with tests and the gate. **No PR without a green gate.**
9. Coordination identifiers (kanban task ids, etc.) stay internal; they never
   appear in user-facing code, comments, or UI text — user→actor language.
10. The repo is public; `stable` and `unstable` have branch protection.
    `unstable` (default) is the contribution line: **PRs merge there after
    review**. `stable` is the maintainer-only release line: changes reach
    it via a **direct fast-forward push** from `unstable`
    (`git push origin unstable:stable`) after a green gate — no merge
    commits, so both branches stay on the identical tip and never drift
    apart. Hard guards stay: `stable` rejects deletions and non-fast-forward
    pushes, so rewriting history on either branch is impossible.

11. **No drive-by refactors.** Files outside the task's scope are not changed;
    architectural changes are neither proposed nor made without an explicit
    user request.

## Gate (before every PR)

```bash
uv run --extra dev pytest -q --tb=short
node --check dashboard/dist/index.js
python3 -m compileall -q dashboard tests
python3 tools/check_secret_safety.py
```

Tests run without a Hermes install, in a clean environment (isolated
imports). Until the whole suite passes, the work is not "done".

## Adding a provider

Code-based model: write a built-in adapter. Step-by-step guide:
[docs/ADDING_A_PROVIDER.md](docs/ADDING_A_PROVIDER.md). Summary:

1. `dashboard/providers/<id>.py`: a `fetch(context)` function + `SPEC =
   ProviderSpec(...)`.
2. Return a normalized card: `base_card(...)` + `windows[]`/`balances[]`
   (helpers in `base.py`).
3. `registry.py`: import + add to `_BUILTIN_SPECS` (order = dashboard card
   layout).
4. `tests/test_<id>.py`: test `fetch` with a fake context (success, no
   credentials, HTTP error, malformed payload, zero balance, no currency).
5. Gate + PR.

## Writing tests

Guide: [docs/WRITING_TESTS.md](docs/WRITING_TESTS.md). Patterns (all
Hermes-free, network-free):

- Provider/helper unit tests: direct import (`dashboard.providers.*`).
- API/settings tests: load modules in isolation with `importlib`
  (`tests/conftest.py` → `plugin_api`, `isolated_settings` fixtures).
- UI bundle tests: call module-scope exports from a Node subprocess
  (`tests/test_bucket_partition.py` pattern).
- Registry tests: monkeypatch `metadata.entry_points` + `_build_registry`
  (see `test_registry.py`).

## Discovery / system facts

- Live plugin checkout: `~/.hermes/plugins/quota-console` (a copy of this
  repo's `stable`; after a release it updates with `git pull --ff-only`).
- Settings storage (runtime):
  `~/.hermes/plugin-data/quota-console/config.json`.

## Conventions

Detailed process: [CONTRIBUTING.md](CONTRIBUTING.md) → §1 (working model) and
§5 (commit standards). Quick reminders:

- Branch: contributions target `unstable` via short-lived branches + PRs;
  after the gate, open the PR and merge on approval (orchestration normally
  runs through kanban workers; the orchestrator merges, no direct pushes).
  `stable` is promoted by the maintainer only, never pushed to directly.
- Commit message: `type(scope): short description` (e.g. `fix: settings
  dialog follows main-screen visibility rules`), English — full standard:
  [CONTRIBUTING.md](CONTRIBUTING.md) → "Commit and message standards".
- New UI controls: within `dashboard/dist/index.js` using the existing `h()`
  pattern; no separate React copy.
