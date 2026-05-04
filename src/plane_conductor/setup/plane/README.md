# `setup/plane/` — bootstrap scripts

Bulk-create the 10 bot accounts, the artifact/role label set, and (optionally)
the decorative `Review` / `Blocked` states in your Plane workspace.

These scripts are **idempotent** — safe to run repeatedly. They are normally
invoked through the CLI:

```bash
plane-conductor setup            # users + labels
plane-conductor setup --states   # also create Review / Blocked states
plane-conductor setup --dry-run  # preview without writes
plane-conductor verify           # smoke check after setup
```

## Files

- `roster.py` — canonical 10-row roster + label/state definitions.
- `create_users.py` — invite each roster row by email.
- `create_labels.py` — create `artifact:*` and `role:*` labels on the project.
- `create_states.py` — optional `Review` / `Blocked` states.
- `runner.py` — orchestrates the above (entry point for `setup` subcommand).
- `verify.py` — read-only check that everything is in place.

## Caveats

- Plane self-hosted needs `ALLOW_NEW_USER_REGISTRATION=1` to invite new
  emails. If your instance disallows new accounts, create the bot users via
  the Plane UI first; the script will then see them and skip the invite.
- Member UUIDs are not auto-saved anywhere — the orchestrator looks them up
  via the API at runtime. If your prompts need the UUIDs hard-coded
  (e.g. for `<mention-component>`), copy them into `plane-config.local.md`.
