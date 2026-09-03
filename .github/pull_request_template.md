## Summary

<!-- What this change does, in a few sentences. -->

> **Target branch:** this repository's contribution line is `unstable` (the
> default). PRs against `stable` are closed automatically — `stable` is
> maintainer-only.

## Why

<!-- Why this change is needed. -->

## Validation

<!-- How this change was validated: tests run, CI status, manual checks. -->

## Checklist

- [ ] All four gate commands pass locally (see [CONTRIBUTING.md](CONTRIBUTING.md) §3)
- [ ] Tests cover the changed behavior (red→green)
- [ ] Docs updated where behavior changed (English only)
- [ ] `project.version` bumped in `pyproject.toml` if this is a behavior change
- [ ] No unrelated changes in this PR
- [ ] No credential values, endpoint URLs, or raw provider payloads added
