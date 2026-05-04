# Agent prompts

Plane Conductor does **not** ship agent prompts. Each agent's behaviour
is defined by a Claude Code prompt file (`<role>.md`) loaded from the
directory you point at via the `PROMPTS_DIR` environment variable.

The mapping is **whatever you put in `conductor.yaml`** — there's no
fixed roster baked into the orchestrator. Each agent entry says
`prompt_role: <stem>`, and Plane Conductor invokes
`claude --agent <nickname> --print` expecting `<stem>.md` to exist in
`PROMPTS_DIR`.

## Example layout

For the shipped [`examples/sdlc-conductor.yaml`](../examples/sdlc-conductor.yaml)
(10 SDLC roles), `PROMPTS_DIR` should contain:

```
business-analyst.md
system-analyst.md
architect.md
designer.md
python-developer.md
vue-developer.md
react-developer.md
api-tester.md
ui-tester.md
reviewer.md
```

For the [`examples/minimal-conductor.yaml`](../examples/minimal-conductor.yaml)
(1 dev agent), just:

```
developer.md
```

## What goes in a prompt file

Each `.md` is a Claude Code agent definition (frontmatter + body). At
minimum:

```markdown
---
name: castor
description: Business analyst — elicits requirements and structures them
  per BABOK v3.
model: claude-sonnet-4-6
tools: Read, Write, Edit, Glob, Grep, Bash, mcp__plane__*
---

# Business Analyst

You take a rough draft from the human and produce a structured
REQUIREMENTS document. Always start by reading the issue you were
mentioned in (UUID is in the prompt piped to you on stdin), then …
```

See [Claude Code docs on agents](https://docs.claude.com/en/docs/claude-code/sub-agents)
for the full frontmatter spec.

## Re-entry

Your prompt is responsible for the continuation/rework logic — Plane
Conductor will spawn the same agent again every time it's mentioned, with
no memory of prior runs. Inside the prompt, check for an existing
sub-issue / artifact in Plane and decide whether to keep going, redo, or
just acknowledge. Plane MCP server (e.g. `makeplane/plane-mcp-server`)
exposes the read/write tools you need.
