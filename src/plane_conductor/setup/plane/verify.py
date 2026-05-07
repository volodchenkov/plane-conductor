"""Smoke check for `plane-conductor verify`."""

from __future__ import annotations

import sys

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.exceptions import PlaneAPIError
from plane_conductor.plane_client import PlaneClient
from plane_conductor.setup.plane.add_project_members import (
    email_to_member_id,
    existing_member_ids,
)
from plane_conductor.setup.plane.create_users import _existing_emails


def _print(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


async def run_verify(workspace: WorkspaceConfig) -> int:
    """Return 0 if all configured agents + labels are present in Plane, 1 otherwise."""
    async with PlaneClient(
        workspace.plane_base_url,
        workspace.plane_api_key,
        workspace.workspace_slug,
    ) as plane:
        try:
            await plane.ping()
        except PlaneAPIError as exc:
            _print(f"FAIL workspace: {exc}")
            return 1
        _print(f"OK   workspace '{workspace.workspace_slug}' reachable")

        problems = 0
        try:
            members = await plane.list_workspace_members()
        except PlaneAPIError as exc:
            _print(f"FAIL members: {exc}")
            return 1
        emails = _existing_emails(members)
        email_to_id = email_to_member_id(members)

        for agent in workspace.agents:
            email = f"{agent.nickname}@{workspace.email_domain}".lower()
            if email in emails:
                _print(f"OK   user {agent.nickname:<10} ({email})")
            else:
                _print(f"MISS user {agent.nickname:<10} ({email})")
                problems += 1

        try:
            project_members = await plane.list_project_members(workspace.project_id)
        except PlaneAPIError as exc:
            _print(f"FAIL project members: {exc}")
            return 1
        in_project = existing_member_ids(project_members)

        for agent in workspace.agents:
            email = f"{agent.nickname}@{workspace.email_domain}".lower()
            member_id = email_to_id.get(email)
            if member_id is None:
                # Already reported above as a workspace MISS — don't double-count.
                continue
            if member_id in in_project:
                _print(f"OK   project-member {agent.nickname:<10} ({email})")
            else:
                _print(f"MISS project-member {agent.nickname:<10} ({email})")
                problems += 1

        try:
            project_labels = await plane.list_labels(workspace.project_id)
        except PlaneAPIError as exc:
            _print(f"FAIL labels: {exc}")
            return 1
        label_names = {str(lbl.get("name", "")).lower() for lbl in project_labels}

        for lbl in workspace.all_labels():
            if lbl.name.lower() in label_names:
                _print(f"OK   label {lbl.name}")
            else:
                _print(f"MISS label {lbl.name}")
                problems += 1

        if problems:
            _print(f"\n{problems} problem(s) found")
            return 1
        _print("\nall good")
        return 0
