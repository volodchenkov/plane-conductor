"""Gate notifications to a workbench instance: detector, failure spool, sender.

A Plane *gate* is a comment written by tower's ``request_handoff(target_role='initiator', …)``:
the first mention is the initiator and the author is somebody else. The webhook handler turns it
into one event and writes it to a spool file **before** it returns (a local write); a
lifecycle-managed sender task delivers spool files to ``POST {url}/workbench/ingest/event``.

Delivery is at-least-once: workbench dedupes on ``(source, kind, event_id)`` and ``event_id`` is the
Plane comment id. Nothing here can fail or delay a Plane webhook.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import ipaddress
import json
import os
import random
import re
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.config import Settings
from plane_conductor.exceptions import PlaneAPIError
from plane_conductor.logging_config import get_logger
from plane_conductor.plane_client import PlaneClient

log = get_logger(__name__)

MAX_REQUEST_BYTES = 7 * 1024
MAX_TITLE = 200
MAX_TEXT = 3500
MAX_URL = 500
BACKOFF_START = 30.0
BACKOFF_CAP = 300.0
ERROR_LOG_EVERY = 600.0
RESOLVE_TIMEOUT = 15.0
ROOT_CACHE_MAX = 512
ROOT_CACHE_TTL = 600.0  # seconds; an issue can be reparented, so a cached root goes stale
_TAG_RE = re.compile(r"<[^>]+>")


def is_loopback_host(host: str) -> bool:
    """Only literals in 127.0.0.0/8 and ``::1`` count; ``localhost`` is resolved by name, so no."""
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class NotifyConfig:
    """Validated notification settings (None from :func:`load_config` = off)."""

    url: str
    token_file: Path | None
    timeout: float
    connect_timeout: float
    ca_file: Path | None
    spool_dir: Path
    spool_max: int
    spool_max_age: int


def load_config(settings: Settings) -> NotifyConfig | None:
    """Return the config, or None when notifications are off or the URL is not acceptable.

    A non-https URL to a non-loopback host is a configuration error: it is logged and notify stays
    off; the conductor still starts.
    """
    raw = settings.workbench_notify_url.strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    host = parts.hostname or ""
    if parts.scheme not in {"http", "https"} or not host:
        log.error("workbench_notify_config_error", reason="invalid url")
        return None
    if parts.scheme != "https" and not is_loopback_host(host):
        log.error(
            "workbench_notify_config_error",
            reason="plain http is allowed only for a loopback literal host",
            host=host,
        )
        return None
    spool = settings.workbench_notify_spool_dir or (settings.log_dir / "workbench-notify-spool")
    return NotifyConfig(
        url=raw.rstrip("/"),
        token_file=settings.workbench_notify_token_file,
        timeout=settings.workbench_notify_timeout,
        connect_timeout=settings.workbench_notify_connect_timeout,
        ca_file=settings.workbench_notify_ca_file,
        spool_dir=spool,
        spool_max=settings.workbench_notify_spool_max,
        spool_max_age=settings.workbench_notify_spool_max_age,
    )


# ------------------------------------------------------------------ gate detector (producer)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fit_bytes(event: dict[str, Any]) -> dict[str, Any]:
    """Shorten ``text`` (by characters, with an ellipsis) until the request fits MAX_REQUEST_BYTES."""
    text = str(event["text"])
    while len(json.dumps(event, ensure_ascii=False).encode()) > MAX_REQUEST_BYTES and text:
        text = _cut(text, max(0, len(text) - max(16, len(text) // 8)))
        event = {**event, "text": text}
    return event


def _build_event(
    kind: str,
    title: str,
    comment_id: str,
    comment_html: str,
    workspace: str,
    issue: str,
    base_url: str,
    project: str,
) -> dict[str, Any]:
    # Plain text for a Telegram notice: structure is irrelevant (unlike mcp_tower.html_to_markdown,
    # which feeds an agent and lives in the MCP layer), but entities must be decoded.
    text = re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", comment_html))).strip()
    url: str | None = f"{base_url.rstrip('/')}/{workspace}/projects/{project}/issues/{issue}"
    if url is None or not url.startswith("https://") or len(url) > MAX_URL:
        url = None
    event: dict[str, Any] = {
        "v": 1,
        "source": "plane",
        "kind": kind,
        "event_id": str(comment_id)[:128],
        "title": _cut(title, MAX_TITLE),
        "text": _cut(text, MAX_TEXT),
        "url": url,
    }
    return _fit_bytes(event)


def detect_gate(
    comment_id: str | None,
    comment_html: str,
    mentions: list[UUID],
    actor: str | None,
    initiator_uuid: UUID,
    workspace: str,
    issue: str,
    base_url: str,
    project: str,
) -> dict[str, Any] | None:
    """The ingest body for a gate comment, or None when the comment is not a gate."""
    if not comment_id or (actor and str(actor).lower() == str(initiator_uuid).lower()):
        return None
    if not mentions or mentions[0] != initiator_uuid:
        return None
    return _build_event(
        "gate",
        f"Plane gate: {workspace}",
        comment_id,
        comment_html,
        workspace,
        issue,
        base_url,
        project,
    )


def detect_handoff(
    comment_id: str | None,
    comment_html: str,
    mentions: list[UUID],
    actor: str | None,
    initiator_uuid: UUID,
    workspace: str,
    issue: str,
    base_url: str,
    project: str,
) -> dict[str, Any] | None:
    """The ingest body for a role-to-role handoff (a progress line), or None.

    A handoff is a comment whose first mention is somebody other than the initiator, written by
    somebody else than the initiator and than the mentioned member (a self-mention is no handoff).
    """
    if not comment_id or not mentions or mentions[0] == initiator_uuid:
        return None
    author = str(actor).lower() if actor else ""
    if author in {str(initiator_uuid).lower(), str(mentions[0]).lower()}:
        return None
    return _build_event(
        "handoff",
        f"Plane handoff: {workspace}",
        comment_id,
        comment_html,
        workspace,
        issue,
        base_url,
        project,
    )


RootResolver = Callable[[str, str, str], Awaitable[str | None]]
"""``(workspace, project, issue) -> root issue id``; raises PlaneAPIError (404 = issue is gone)."""


def make_root_resolver(clients: dict[str, tuple[WorkspaceConfig, PlaneClient]]) -> RootResolver:
    """A resolver over the conductor's Plane clients (``slug -> (WorkspaceConfig, PlaneClient)``).

    A sub-issue resolves to its parent, a root to itself. Runs in the sender task only, never in
    the webhook handler.
    """

    async def resolve(workspace: str, project: str, issue: str) -> str | None:
        if workspace not in clients:
            return None
        plane = clients[workspace][1]
        data = await plane.get_issue(project, issue)
        parent = data.get("parent")
        root = parent if isinstance(parent, str) and parent else data.get("id") or issue
        return str(root)

    return resolve


# ------------------------------------------------------------------ spool + sender


class Notifier:
    """Failure spool and sender for one conductor process."""

    def __init__(
        self,
        cfg: NotifyConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        jitter: Callable[[], float] = lambda: random.uniform(0.0, 0.2),
    ) -> None:
        self.cfg = cfg
        self._transport = transport
        self._clock = clock
        self._jitter = jitter
        self.wake = asyncio.Event()
        self.backoff = BACKOFF_START
        self.next_try = 0.0
        self.last_error_log = 0.0
        self._task: asyncio.Task[None] | None = None
        self._seq = 0
        self.resolver: RootResolver | None = None
        self._roots: dict[tuple[str, str], tuple[float, str | None]] = {}

    # ---- spool ----------------------------------------------------------------
    def publish(self, event: dict[str, Any], ctx: dict[str, str] | None = None) -> bool:
        """Write the event to the spool (tmp + rename) and wake the sender. Never raises.

        With ``ctx`` (workspace, project, issue) the file is an envelope ``{"event", "ctx"}`` and
        the sender resolves the root issue id before delivery; without it the file is the plain
        body (also what a spool written by an older conductor looks like).
        """
        try:
            self.cfg.spool_dir.mkdir(parents=True, exist_ok=True)
            self._seq += 1
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(event["event_id"]))[:64]
            name = f"{time.time_ns():020d}-{self._seq:06d}-{safe}.json"
            tmp = self.cfg.spool_dir / f".{name}.tmp"
            doc = {"event": event, "ctx": ctx} if ctx else event
            tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.cfg.spool_dir / name)
            self._enforce_bounds()
        except OSError as exc:
            log.error("workbench_notify_spool_write_failed", error=type(exc).__name__)
            return False
        self.wake.set()
        return True

    def files(self) -> list[Path]:
        try:
            return sorted(p for p in self.cfg.spool_dir.glob("*.json") if p.is_file())
        except OSError:
            return []

    def _enforce_bounds(self) -> None:
        now = self._clock()
        dropped = 0
        files = self.files()
        for path in list(files):
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if age > self.cfg.spool_max_age:
                path.unlink(missing_ok=True)
                files.remove(path)
                dropped += 1
        while len(files) > self.cfg.spool_max:
            files.pop(0).unlink(missing_ok=True)
            dropped += 1
        if dropped:
            log.error("workbench_notify_spool_dropped", count=dropped)

    # ---- sending --------------------------------------------------------------
    def _token(self) -> str | None:
        if self.cfg.token_file is None:
            return None
        try:
            return self.cfg.token_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _client(self) -> httpx.AsyncClient:
        verify: Any = (
            ssl.create_default_context(cafile=str(self.cfg.ca_file)) if self.cfg.ca_file else True
        )
        return httpx.AsyncClient(
            transport=self._transport,
            verify=verify,
            follow_redirects=False,  # the bearer never leaves the configured host
            timeout=httpx.Timeout(self.cfg.timeout, connect=self.cfg.connect_timeout),
        )

    async def send_pass(self) -> int:
        """One pass, oldest first; stops at the first failure. Returns the files delivered."""
        self._enforce_bounds()
        delivered = 0
        try:
            client_cm = self._client()
        except (OSError, ssl.SSLError) as exc:  # unreadable / invalid private CA file
            self._schedule_retry(type(exc).__name__)
            return 0
        async with client_cm as client:
            for path in self.files():
                outcome = await self._send_file(client, path)
                if outcome == "ok":
                    delivered += 1
                    self.backoff = BACKOFF_START
                    continue
                if outcome == "dropped":
                    continue
                self._schedule_retry(outcome)
                return delivered
        self.next_try = 0.0
        return delivered

    async def _send_file(self, client: httpx.AsyncClient, path: Path) -> str:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            log.error("workbench_notify_bad_spool_file")
            path.unlink(missing_ok=True)
            return "dropped"
        body, ctx = (doc["event"], doc.get("ctx")) if "event" in doc else (doc, None)
        if not isinstance(body, dict):
            log.error("workbench_notify_bad_spool_file")
            path.unlink(missing_ok=True)
            return "dropped"
        if isinstance(ctx, dict) and self.resolver is not None:
            try:
                root = await self._root_of(ctx)
            except PlaneAPIError as exc:  # transient, auth, rate limit: keep the file, retry later
                return f"plane_{exc.status_code}"
            except Exception as exc:  # hanging / unexpected lookup: same
                return f"plane_{type(exc).__name__}"
            if root:
                body = {**body, "root_id": root}
        token = self._token()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = await client.post(
                f"{self.cfg.url}/workbench/ingest/event", json=body, headers=headers
            )
        except httpx.HTTPError as exc:  # connection, DNS, TLS, timeout
            return type(exc).__name__
        status = resp.status_code
        if status == 202:
            path.unlink(missing_ok=True)
            return "ok"
        if status in (400, 413):  # permanent: the body is wrong, retrying cannot help
            log.error("workbench_notify_rejected", status=status, event_id=body.get("event_id"))
            path.unlink(missing_ok=True)
            return "dropped"
        # 401/403/404/429/5xx and redirects: an operator fixes these; deleting would lose the gate
        return f"http_{status}"

    async def _root_of(self, ctx: dict[str, str]) -> str | None:
        """Root issue id for the envelope; None when the issue is gone (404 → sent unbound)."""
        assert self.resolver is not None
        key = (str(ctx.get("workspace")), str(ctx.get("issue")))
        hit = self._roots.get(key)
        if hit is not None and time.monotonic() - hit[0] < ROOT_CACHE_TTL:
            return hit[1]
        try:
            root = await asyncio.wait_for(
                self.resolver(key[0], str(ctx.get("project")), key[1]), timeout=RESOLVE_TIMEOUT
            )
        except PlaneAPIError as exc:
            if exc.status_code != 404:
                raise
            root = None
        self._roots.pop(key, None)
        if len(self._roots) >= ROOT_CACHE_MAX:
            self._roots.pop(next(iter(self._roots)))
        self._roots[key] = (time.monotonic(), root)
        return root

    def _schedule_retry(self, reason: str) -> None:
        now = self._clock()
        self.next_try = now + self.backoff * (1 + self._jitter())
        self.backoff = min(BACKOFF_CAP, self.backoff * 2)
        if now - self.last_error_log >= ERROR_LOG_EVERY:
            self.last_error_log = now
            log.error("workbench_notify_failing", reason=reason, spooled=len(self.files()))

    # ---- lifecycle ------------------------------------------------------------
    async def run(self) -> None:
        while True:
            if self.files() and self._clock() >= self.next_try:
                try:
                    await self.send_pass()
                except Exception as exc:  # the sender must never die
                    log.error("workbench_notify_pass_failed", error=type(exc).__name__)
                    self._schedule_retry("internal")
            self.wake.clear()
            wait = 1.0
            if self.next_try > self._clock():
                wait = min(5.0, max(0.1, self.next_try - self._clock()))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=wait)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


def build_notifier(settings: Settings) -> Notifier | None:
    """A notifier for the configured URL, or None when notifications are off/invalid."""
    cfg = load_config(settings)
    return Notifier(cfg) if cfg is not None else None
