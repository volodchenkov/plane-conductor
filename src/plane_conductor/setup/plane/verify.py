"""Smoke check for `plane-conductor verify`."""

from __future__ import annotations

import sys

from plane_conductor.conductor_config import ConductorConfig
from plane_conductor.config import Settings
from plane_conductor.exceptions import PlaneAPIError
from plane_conductor.plane_client import PlaneClient
from plane_conductor.setup.plane.create_users import _existing_emails


def _print(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


async def run_verify(settings: Settings, config: ConductorConfig) -> int:
    """Return 0 if all configured agents + labels are present in Plane, 1 otherwise."""
    async with PlaneClient(
        settings.plane_base_url,
        settings.plane_api_key,
        settings.plane_workspace_slug,
    ) as plane:
        try:
            await plane.ping()
        except PlaneAPIError as exc:
            _print(f"FAIL workspace: {exc}")
            return 1
        _print(f"OK   workspace '{settings.plane_workspace_slug}' reachable")

        problems = 0
        try:
            members = await plane.list_workspace_members()
        except PlaneAPIError as exc:
            _print(f"FAIL members: {exc}")
            return 1
        emails = _existing_emails(members)

        for agent in config.agents:
            email = f"{agent.nickname}@{settings.email_domain}".lower()
            if email in emails:
                _print(f"OK   user {agent.nickname:<10} ({email})")
            else:
                _print(f"MISS user {agent.nickname:<10} ({email})")
                problems += 1

        try:
            project_labels = await plane.list_labels(settings.plane_project_id)
        except PlaneAPIError as exc:
            _print(f"FAIL labels: {exc}")
            return 1
        label_names = {str(lbl.get("name", "")).lower() for lbl in project_labels}

        for lbl in config.all_labels():
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
