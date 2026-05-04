"""Mutating e2e — creates a unique label and verifies it's listed.

Gated by `PLANE_E2E_MUTATING=1` on top of `PLANE_E2E=1`. The test client does
not delete the label (PlaneClient has no delete_label method and we don't add
one just for tests); the leftover is visible and safe to remove via the Plane
UI. Names are prefixed `pcond-e2e-` for easy identification.
"""

from __future__ import annotations

import os
import uuid
from uuid import UUID

import pytest

from plane_conductor.plane_client import PlaneClient


@pytest.fixture(autouse=True)
def _mutating_gate() -> None:
    if os.environ.get("PLANE_E2E_MUTATING") != "1":
        pytest.skip("set PLANE_E2E_MUTATING=1 to run mutating e2e")


async def test_create_label_visible_in_list(plane: PlaneClient, plane_project_id: UUID) -> None:
    name = f"pcond-e2e-{uuid.uuid4().hex[:8]}"
    created = await plane.create_label(plane_project_id, name, color="#999999")
    assert created.get("name") == name

    labels = await plane.list_labels(plane_project_id)
    assert any(lbl.get("name") == name for lbl in labels)
