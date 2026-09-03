# Security Policy

## Scope

This project is a plugin that runs inside the Hermes dashboard. Provider
credentials and Hermes profile state must stay on the plugin server; only the
summary fields needed for display are sent to the browser.

## Security rules

- Tokens, API keys, refresh tokens, `auth.json`, `.env`, or real `config.yaml`
  files are never committed.
- Provider requests go to fixed, allow-listed official hosts only.
- Requests carrying bearer credentials never follow redirects.
- Provider response bodies, account identifiers, and raw exception text are
  never logged.
- API responses never contain credential values, account identifiers, or raw
  provider payloads.
- If Hermes APIs are missing or incompatible, the plugin fails closed with safe
  defaults; no guessing about the missing API surface.
- Reset never deletes credential values and never copies credentials between
  profiles.

## Reporting a vulnerability

Do not report vulnerabilities in public issues. Send details to the repository
owner via a GitHub private security advisory or a direct secure channel.

Never include real tokens, API keys, or credential values in a report; replace
them with `[REDACTED]` where needed.
