# End-to-end tests

These tests hit a **real** Plane instance. They are skipped unless
`PLANE_E2E=1` is set in the environment, so CI never runs them.

## Run locally

```bash
# Required env (same set as the orchestrator itself):
export PLANE_BASE_URL=https://plane.example.io
export PLANE_API_KEY=plane_api_xxxxxxxxxxxxxxxxxxxxxxxx
export PLANE_WORKSPACE_SLUG=your-workspace
export PLANE_PROJECT_ID=00000000-0000-0000-0000-000000000000

# Enable e2e:
export PLANE_E2E=1

# Run only the e2e suite:
.venv/bin/pytest tests/e2e -v
```

## What gets tested

- **Read-only smoke** (`test_read_only.py`) — confirms Plane is reachable, the
  workspace exists, and `list_workspace_members` / `list_labels` /
  `list_states` return data. Safe to run anywhere; nothing is created.
- **Idempotent setup roundtrip** (`test_setup_roundtrip.py`) — calls the same
  `setup/plane/*` flows the CLI does, in `dry_run=True` mode only. Confirms
  the bot users / labels / states are visible without making changes.
- **Mutating roundtrip** (`test_label_create_delete.py`) — gated additionally
  by `PLANE_E2E_MUTATING=1`. Creates a uniquely-named label, lists it,
  deletes it. Use this to confirm write access really works.

## Cleanup

The mutating test cleans up after itself. If a run is interrupted, leftover
labels are named `pcond-e2e-<random>` so they're easy to find.
