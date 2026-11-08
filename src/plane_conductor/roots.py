"""Root-issue resolution with a short cache (QSALE-55 SPEC §3.3).

The dedupe key of a run is ``(workspace, nickname, root issue)``; a sub-issue and its
root must therefore map to the *same* root id. The root is the top-level ancestor
(walk ``parent``, cycle guard, depth <= 10). A failure never invents a key: the caller
keeps the trigger and retries on the next tick.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.exceptions import PlaneAPIError, RootUnresolvedError
from plane_conductor.plane_client import PlaneClient

ROOT_CACHE_TTL = 600.0
ROOT_CACHE_MAX = 2048
MAX_DEPTH = 10


class RootCache:
    """``(workspace, issue) -> root id`` over the per-workspace Plane clients."""

    def __init__(
        self,
        clients: dict[str, tuple[WorkspaceConfig, PlaneClient]],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        #: slug -> (WorkspaceConfig, PlaneClient)
        self._clients = clients
        self._clock = clock
        self._cache: dict[tuple[str, str], tuple[float, str]] = {}

    async def root_of(
        self, workspace: str, project_id: str, issue_id: str, *, fresh: bool = False
    ) -> str:
        """Top-level ancestor of ``issue_id``. Raises ``PlaneAPIError`` / ``RootUnresolvedError``.

        ``fresh=True`` skips the cache (used right before a start: the issue may have been
        re-parented while its trigger waited).
        """
        key = (workspace, issue_id)
        hit = self._cache.get(key)
        if not fresh and hit is not None and self._clock() - hit[0] < ROOT_CACHE_TTL:
            return hit[1]
        plane: PlaneClient = self._clients[workspace][1]
        seen: set[str] = set()
        current = issue_id
        for _ in range(MAX_DEPTH):
            if current in seen:
                raise RootUnresolvedError(f"parent cycle at {current}")
            seen.add(current)
            data = await plane.get_issue(project_id, current)
            parent = data.get("parent")
            if isinstance(parent, dict):  # some Plane builds expand the parent
                parent = parent.get("id")
            if not parent:
                self._store(key, current)
                return current
            current = str(parent)
        raise RootUnresolvedError(f"parent chain deeper than {MAX_DEPTH}")

    def _store(self, key: tuple[str, str], root: str) -> None:
        if len(self._cache) >= ROOT_CACHE_MAX:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = (self._clock(), root)

    def forget(self, workspace: str, issue_id: str) -> None:
        self._cache.pop((workspace, issue_id), None)


def is_gone(exc: PlaneAPIError) -> bool:
    """404/403: the issue is deleted or the access revoked — not worth retrying."""
    return exc.status_code in (403, 404)
