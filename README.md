# Plane Conductor

Webhook orchestrator that turns Plane mentions into Claude Code agent runs — full SDLC pipeline (analyst, architect, coders, testers, reviewer) triggered from issue comments.

> **Status:** bootstrap. See [`REQUIREMENTS.md`](./REQUIREMENTS.md) for what we're building and [`SPEC.md`](./SPEC.md) for how. The implementation phase begins next.

---

## What is this?

You have a [Plane](https://plane.so) workspace and [Claude Code](https://claude.com/claude-code). Plane Conductor is the bridge between them. Mention a bot user (`@sark`, `@rinzler`, etc.) in a Plane issue comment — Plane Conductor receives the webhook, resolves the mention to an agent role, and spawns a Claude Code session to do the work.

The result is a full software development pipeline you operate from a task tracker:

```
@castor   →  business analyst (gathers requirements)
@sark     →  system analyst (writes spec)
@flynn    →  architect (reviews spec, approves)
@quorra   →  designer (designs UI/UX)
@rinzler  →  python developer
@ram      →  vue developer
@beck     →  react developer
@yori     →  API tester
@gem      →  UX/E2E tester
@dumont   →  final reviewer
```

Each agent reads the Plane issue, performs its role, posts results back as a sub-issue with artifact and a summary comment.

---

## Quickstart

> Coming with v0.1 release. The implementation is in progress — see [`SPEC.md`](./SPEC.md).

```bash
pip install plane-conductor
cp .env.example .env
# edit .env with your Plane API key, base URL, workspace slug
plane-conductor setup     # creates bot users, labels in your Plane workspace
plane-conductor serve     # starts the webhook server
```

Point Plane webhook at `https://your-host/webhook`, add the secret to `.env`, mention `@sark` in any issue — and watch the agent work.

---

## Documentation

- [`REQUIREMENTS.md`](./REQUIREMENTS.md) — product requirements (what and why)
- [`SPEC.md`](./SPEC.md) — technical specification (how)
- `docs/` — full documentation site (coming)

---

## License

[MIT](./LICENSE) © 2026 Dmitry Volodchenkov
