# Security policy

## Reporting a vulnerability

If you find a security issue in Plane Conductor — please **do not open a
public GitHub issue**. Instead, email the maintainer directly:

- Dmitry Volodchenkov — `volodchenkov@gmail.com`

Include:
- A description of the issue and the impact you assessed.
- Steps to reproduce, or a minimal proof-of-concept.
- Affected version (`plane-conductor --version` or commit hash).

Expect an acknowledgement within **5 working days** and a fix or
mitigation plan within **30 days** for credible reports.

## Scope

Plane Conductor is a single-tenant orchestrator that runs on a host you
control. The realistic threat surface is:

- The `POST /webhook` endpoint exposed to Plane (HMAC-SHA256 verified).
- The `claude` subprocesses it spawns (run as the same user as the
  service, with whatever filesystem and network access that user has).
- The Plane API token, webhook secret, and any third-party credentials
  configured in `/etc/plane-conductor/.env`.

Out of scope:
- Vulnerabilities in upstream `claude` CLI, `plane-mcp-server`, or Plane
  itself — please report to those projects directly.
- Issues that require local root access on the host already (the
  attacker has read access to `/etc/plane-conductor/.env`, etc.).

## Hardening reminders

- Generate `WEBHOOK_SECRET` with `openssl rand -hex 32` and never reuse
  it across instances.
- `/etc/plane-conductor/.env` should be `chmod 640`, owned `root:<service-group>`.
- Run `plane-conductor` behind a reverse proxy that terminates TLS; the
  built-in server is HTTP-only.
- Restrict the bot accounts to the **single project** they need to act
  in (workspace member ≠ admin).
- Set `ALLOWED_NICKNAMES` to a strict whitelist if you don't want every
  configured agent reachable via mention.
- Cap `MAX_CONCURRENT_SESSIONS` and `SESSION_TIMEOUT_SECONDS` to values
  matching your machine and budget.
