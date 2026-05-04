"""Fixtures + skip-gates for the e2e suite."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import UUID

import pytest

from plane_conductor.plane_client import PlaneClient


def _skip_unless(env_var: str) -> None:
    if os.environ.get(env_var) != "1":
        pytest.skip(f"e2e test skipped (set {env_var}=1 to run)", allow_module_level=False)


@pytest.fixture(autouse=True)
def _e2e_gate() -> None:
    _skip_unless("PLANE_E2E")


@pytest.fixture
def plane_base_url() -> str:
    v = os.environ.get("PLANE_BASE_URL")
    if not v:
        pytest.skip("PLANE_BASE_URL not set")
    return v.rstrip("/")


@pytest.fixture
def plane_api_key() -> str:
    v = os.environ.get("PLANE_API_KEY")
    if not v:
        pytest.skip("PLANE_API_KEY not set")
    return v


@pytest.fixture
def plane_workspace_slug() -> str:
    v = os.environ.get("PLANE_WORKSPACE_SLUG")
    if not v:
        pytest.skip("PLANE_WORKSPACE_SLUG not set")
    return v


@pytest.fixture
def plane_project_id() -> UUID:
    v = os.environ.get("PLANE_PROJECT_ID")
    if not v:
        pytest.skip("PLANE_PROJECT_ID not set")
    return UUID(v)


@pytest.fixture
async def plane(
    plane_base_url: str, plane_api_key: str, plane_workspace_slug: str
) -> AsyncIterator[PlaneClient]:
    async with PlaneClient(plane_base_url, plane_api_key, plane_workspace_slug) as client:
        yield client
