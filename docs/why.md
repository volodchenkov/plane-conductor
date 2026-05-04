# Why Plane Conductor

## The problem

You want a multi-stage workflow where each stage is an LLM agent —
analyst writes requirements, architect reviews, devs code, testers test,
reviewer approves. You want the trigger to be a normal **comment in your
task tracker**, not a Slack message or a CLI invocation. And you want
the agents to run **on your own machine** so they see your filesystem,
your `~/.claude.json`, your git repos.

You can wire this together yourself. People do — every time the same
way:

1. Spin up a webhook server.
2. Verify the signature.
3. Parse the mention.
4. Resolve the user.
5. `subprocess.Popen` something.
6. … later, fix the race condition where two webhooks for the same
   issue arrive within a second.
7. … later, fix the leak where the subprocess outlives a restart.
8. … later, add a capacity cap because one mention storm took the box
   down.
9. … later, realise the subprocess hung and nobody noticed for three
   hours.

Plane Conductor is **steps 1–9 already done**. It's not novel — it's
the polished version of a script every team writes.

---

## vs. alternatives

| Tool | Can it do this? | Cost |
|---|---|---|
| **n8n** | Yes — Webhook trigger + Execute Command node + Function nodes for parsing. | A day's work to build, plus you write the resilience patterns yourself (process group kill, sentinel-recovery, dedup, capacity cap). Visual flow is a bonus for non-engineers, overkill for a one-action workflow. |
| **Activepieces / Trigger.dev / Inngest** | No (for the local-subprocess case). | Workflows run in *their* runtime — no access to your local filesystem or `~/.claude.json`. Fine for API-to-API automation, wrong shape for "agent on my box". |
| **GitHub Actions self-hosted runner** | Yes, awkwardly. | Listener wired to a Plane webhook can dispatch a workflow run to a self-hosted runner. Heavy: needs a GH repo, a runner daemon, a workflow YAML per agent. Resilience is your problem. |
| **Plain FastAPI + 200 lines** | Yes, until it isn't. | This *is* Plane Conductor v0.1 — minus tests, minus the resilience patterns, minus the YAML config, minus the setup tooling. Every team writes their own version and re-discovers the same race conditions. |
| **Slack / Discord bot** | Different problem. | Tied to chat, not a task tracker. No persistent issue/sub-issue artifacts. |

The honest summary: **nothing else hits exactly this combination**
(task tracker as UI + local subprocess + production-grade lifecycle
management + zero infrastructure beyond a single binary). For each
alternative there's a use case where it's the right answer; this isn't
that use case.

---

## When to use Plane Conductor

- You're already using **Plane** (cloud or self-hosted) for tasks.
- You want **Claude Code** agents (or anything you'd `claude --agent X
  --print`) as your runtime.
- You want them to live on **your machine** — your files, your secrets,
  your repos.
- You're fine with **one orchestrator per workspace** (no multi-tenant
  needs).

## When *not* to use it

- Your agents need to live in the cloud (no local FS access required).
  Use Trigger.dev, Inngest, or n8n in cloud mode.
- You want a visual flow editor for non-engineers to maintain. Use n8n.
- You need multiple orchestrators sharing state across hosts. You'd
  need to add Redis + a coordinator yourself; we're not that yet.
- Your "agent" is really a single API call. You don't need orchestration
  at all — Plane's native webhook → your endpoint is enough.

---

## What Plane Conductor is *not*

- **Not an agent framework.** It doesn't define what your agents do, how
  they remember, how they collaborate. That's the agent's prompt + the
  Plane MCP server's tools. Plane Conductor only spawns processes.
- **Not a workflow engine.** It doesn't have nodes, branches, retries,
  or DAGs. The "workflow" is your team's natural conversation in Plane.
- **Not a Plane replacement.** Plane stays the source of truth for tasks,
  artifacts, and conversation. Plane Conductor is the dispatcher.
- **Not multi-tenant.** One process serves one workspace.

---

## Honest status

Plane Conductor is a **specific glue** for a specific niche. It does
that one job well — production patterns, decent test coverage, clean
config story. It will not transform your business or replace your eng
team. It will let you have an LLM analyst draft requirements while you
sleep, and not lose half of them to a hung subprocess.

That's the pitch.
