"""End-to-end setup orchestration (called by `plane-conductor setup`)."""

from __future__ import annotations

import sys

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.exceptions import PlaneAPIError
from plane_conductor.logging_config import get_logger
from plane_conductor.plane_client import PlaneClient
from plane_conductor.setup.plane.create_labels import create_labels as create_labels_step
from plane_conductor.setup.plane.create_states import create_states as create_states_step
from plane_conductor.setup.plane.create_users import invite_roster as invite_roster_step

log = get_logger(__name__)


def _print(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _summarise(title: str, statuses: dict[str, str]) -> int:
    failed = sum(1 for v in statuses.values() if v == "failed")
    created = sum(1 for v in statuses.values() if v in {"created", "invited"})
    exists = sum(1 for v in statuses.values() if v == "exists")
    _print(f"{title}: created={created} existed={exists} failed={failed}")
    for name, status in statuses.items():
        marker = {"created": "+", "invited": "+", "exists": ".", "failed": "x"}.get(status, "?")
        _print(f"  {marker} {name} ({status})")
    return failed


async def run_setup(
    workspace: WorkspaceConfig,
    *,
    create_states: bool = False,
    dry_run: bool = False,
) -> int:
    """Run the full setup for one workspace. Returns 0 on success, 1 if any step failed."""
    async with PlaneClient(
        workspace.plane_base_url,
        workspace.plane_api_key,
        workspace.workspace_slug,
    ) as plane:
        try:
            projects = await plane.ping()
        except PlaneAPIError as exc:
            _print(f"cannot reach Plane workspace '{workspace.workspace_slug}': {exc}")
            return 1

        _print(
            f"connected: workspace={workspace.workspace_slug} ({len(projects)} project(s) visible)"
        )
        _print(
            f"agents={len(workspace.agents)} labels={len(workspace.all_labels())} "
            f"states={len(workspace.states)}"
        )

        users_status = await invite_roster_step(plane, workspace, dry_run=dry_run)
        labels_status = await create_labels_step(
            plane, workspace.project_id, workspace, dry_run=dry_run
        )

        states_failed = 0
        if create_states and workspace.states:
            states_status = await create_states_step(
                plane, workspace.project_id, workspace, dry_run=dry_run
            )
            states_failed = _summarise("states", states_status)

        users_failed = _summarise("users", users_status)
        labels_failed = _summarise("labels", labels_status)

        _print("")
        _print(
            "next: bot accounts must accept their invitations (Plane sends one per email). "
            "For fully automated bootstrap see examples/bootstrap-bots.sh."
        )

        return 0 if (users_failed + labels_failed + states_failed) == 0 else 1
