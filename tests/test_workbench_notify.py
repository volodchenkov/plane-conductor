# mypy: disable-error-code="arg-type, dict-item"
"""Gate notifications to workbench (N1-N16): detector, failure spool, sender, URL rules, runner env."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import ssl
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from plane_conductor import workbench_client
from plane_conductor.conductor_config import WorkspaceConfig
from plane_conductor.config import Settings
from plane_conductor.runner import spawn_env
from plane_conductor.webhook import build_router
from plane_conductor.workbench_client import (
    BACKOFF_CAP,
    BACKOFF_START,
    MAX_REQUEST_BYTES,
    Notifier,
    NotifyConfig,
    build_notifier,
    detect_gate,
    is_loopback_host,
    load_config,
)

INIT = UUID("00000000-0000-0000-0000-000000000099")
ISSUE = "33333333-3333-3333-3333-333333333333"


class Recorder:
    """Stands in for the module logger: structlog loggers are cached once any app configured them."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def _add(self, event: str, **kw: Any) -> None:
        self.records.append({"event": event, **kw})

    error = warning = info = debug = _add


@pytest.fixture
def capture_logs(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(workbench_client, "log", rec)
    return rec


def mention(uid: UUID | str, text: str = "needs a decision") -> str:
    return (
        f'<mention-component entity_identifier="{uid}" entity_name="user_mention">'
        f"</mention-component> {text}"
    )


class Plane:
    async def get_member(self, member_id: Any) -> dict[str, Any]:
        from plane_conductor.exceptions import PlaneAPIError

        raise PlaneAPIError(404, f"missing {member_id}")

    async def list_issue_comments(self, project_id: Any, issue_id: Any) -> list[dict[str, Any]]:
        return []


class Runner:
    async def request_spawn(self, **kwargs: Any) -> dict[str, Any]:
        return {"status": "spawned"}


def cfg(tmp_path: Path, **kw: Any) -> NotifyConfig:
    base = {
        "url": "http://127.0.0.1:8780",
        "token_file": tmp_path / "token",
        "timeout": 3.0,
        "connect_timeout": 2.0,
        "ca_file": None,
        "spool_dir": tmp_path / "spool",
        "spool_max": 200,
        "spool_max_age": 24 * 3600,
    }
    (tmp_path / "token").write_text("tok-1\n")
    return NotifyConfig(**(base | kw))


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def make_notifier(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    clock: Clock | None = None,
    **kw: Any,
) -> Notifier:
    return Notifier(
        cfg(tmp_path, **kw),
        transport=httpx.MockTransport(handler),
        clock=clock or Clock(),
        jitter=lambda: 0.0,
    )


def event(event_id: str = "c1", **kw: Any) -> dict[str, Any]:
    return {
        "v": 1,
        "source": "plane",
        "kind": "gate",
        "event_id": event_id,
        "title": "t",
        "text": "x",
        "url": None,
    } | kw


def spool_files(n: Notifier) -> list[Path]:
    return list(n.files())


# ---------------------------------------------------------------- webhook -> spool -> ingest (N1-N5, N10)


def gate_body(comment_id: str = "c1", actor: str = "agent-1", text: str = "approve?") -> bytes:
    return json.dumps(
        {
            "event": "issue_comment",
            "action": "created",
            "data": {
                "id": comment_id,
                "actor": actor,
                "issue": ISSUE,
                "comment_html": mention(INIT, text),
            },
        }
    ).encode()


def post_gate(client: TestClient, workspace: WorkspaceConfig, body: bytes) -> httpx.Response:
    sig = hmac.new(workspace.webhook_secret.encode(), body, sha256).hexdigest()
    return client.post(
        f"/{workspace.workspace_slug}/webhook",
        content=body,
        headers={workspace.webhook_signature_header: sig},
    )


def app_with(settings: Settings, workspace: WorkspaceConfig, notifier: Notifier) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_router(settings, {workspace.workspace_slug: (workspace, Plane())}, Runner(), notifier)
    )
    return TestClient(app)


def test_n1_gate_comment_writes_one_spool_file_before_the_response(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    n = make_notifier(tmp_path, lambda r: httpx.Response(202))
    resp = post_gate(app_with(settings, workspace_config, n), workspace_config, gate_body())
    assert resp.status_code == 200
    (f,) = spool_files(n)  # on disk by the time the response returned
    body = json.loads(f.read_text())
    assert body["v"] == 1 and body["source"] == "plane" and body["kind"] == "gate"
    assert body["event_id"] == "c1" and body["text"] == "approve?"
    assert body["url"].startswith(
        f"https://plane.test/{workspace_config.workspace_slug}/projects/"
    ) and body["url"].endswith(f"/issues/{ISSUE}")
    assert len(body["title"]) <= 200


async def test_n1_sender_calls_ingest_with_expected_body_and_bearer(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(202, json={"accepted": True})

    n = make_notifier(tmp_path, handler)
    n.publish(event())
    assert await n.send_pass() == 1
    (req,) = seen
    assert req.url.path == "/workbench/ingest/event" and req.method == "POST"
    assert req.headers["authorization"] == "Bearer tok-1"
    assert json.loads(req.content)["event_id"] == "c1" and spool_files(n) == []


def test_n2_same_comment_twice_gives_two_spool_files_with_the_same_event_id(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    n = make_notifier(tmp_path, lambda r: httpx.Response(202))
    client = app_with(settings, workspace_config, n)
    post_gate(client, workspace_config, gate_body())
    post_gate(client, workspace_config, gate_body())  # an edit of the same comment
    ids = [json.loads(f.read_text())["event_id"] for f in spool_files(n)]
    assert ids == ["c1", "c1"]


def test_not_a_gate_nothing_spooled(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    n = make_notifier(tmp_path, lambda r: httpx.Response(202))
    client = app_with(settings, workspace_config, n)
    # by the initiator himself
    post_gate(client, workspace_config, gate_body(actor=str(INIT)))
    # mention is not first / not the initiator
    other = json.dumps(
        {
            "event": "issue_comment",
            "action": "created",
            "data": {
                "id": "c9",
                "actor": "a",
                "issue": ISSUE,
                "comment_html": mention("11111111-1111-1111-1111-111111111111"),
            },
        }
    ).encode()
    post_gate(client, workspace_config, other)
    assert spool_files(n) == []


async def test_n3_replay_sends_the_stored_event_unchanged_and_deletes_on_202(
    tmp_path: Path,
) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(202)

    n = make_notifier(tmp_path, handler)
    ev = event("c7", title="Stored", text="exact text", url="https://p.test/i")
    n.publish(ev)
    n2 = make_notifier(tmp_path, handler)  # a fresh process over the same spool dir
    await n2.send_pass()
    assert bodies == [ev] and spool_files(n2) == []


def test_n4_workbench_down_webhook_answers_normally_file_stays(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    n = make_notifier(tmp_path, lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))
    resp = post_gate(app_with(settings, workspace_config, n), workspace_config, gate_body())
    assert resp.status_code == 200 and resp.json()["spawned"] == []
    assert len(spool_files(n)) == 1


async def test_n4_failed_send_keeps_the_file_and_retries_later(tmp_path: Path) -> None:
    calls = []
    clock = Clock()

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(202)

    n = make_notifier(tmp_path, handler, clock)
    n.publish(event())
    assert await n.send_pass() == 0 and len(spool_files(n)) == 1
    clock.now += n.backoff + 1
    assert await n.send_pass() == 1 and spool_files(n) == []


async def test_n5_hanging_endpoint_does_not_delay_the_webhook(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    started = asyncio.Event()

    async def hang(req: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(3600)
        return httpx.Response(202)

    n = Notifier(
        cfg(tmp_path, timeout=0.05, connect_timeout=0.05),
        transport=httpx.MockTransport(hang),
        jitter=lambda: 0.0,
    )
    # the webhook only does a local write; the sender is a separate task and is bounded by its timeout
    assert n.publish(event()) is True
    t0 = asyncio.get_running_loop().time()
    resp = await asyncio.to_thread(
        post_gate, app_with(settings, workspace_config, n), workspace_config, gate_body("c2")
    )
    assert resp.status_code == 200 and asyncio.get_running_loop().time() - t0 < 2
    n.start()
    await asyncio.wait_for(started.wait(), 3)
    await n.stop()
    assert len(spool_files(n)) == 2  # nothing was lost while the endpoint hung


# ---------------------------------------------------------------- permanent vs kept statuses (N6, N16)


@pytest.mark.parametrize("status", [400, 413])
async def test_n6_permanent_rejection_deletes_the_file_without_retry_token_not_logged(
    tmp_path: Path, status: int, capture_logs: Recorder
) -> None:
    n = make_notifier(tmp_path, lambda r: httpx.Response(status))
    n.publish(event())
    await n.send_pass()
    logs = capture_logs.records
    assert spool_files(n) == []
    assert "tok-1" not in json.dumps(logs, default=str)
    assert [r["event"] for r in logs] == ["workbench_notify_rejected"]
    assert n.next_try == 0.0  # no backoff: the rest of the spool keeps flowing


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 502, 503])
async def test_n16_unfixed_by_retrying_statuses_keep_the_file(tmp_path: Path, status: int) -> None:
    n = make_notifier(tmp_path, lambda r: httpx.Response(status))
    n.publish(event())
    await n.send_pass()
    assert len(spool_files(n)) == 1 and n.next_try > 0


async def test_n16_a_redirect_is_not_followed_and_keeps_the_file(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.host)
        return httpx.Response(307, headers={"location": "https://evil.example.test/steal"})

    n = make_notifier(tmp_path, handler)
    n.publish(event())
    await n.send_pass()
    assert seen == ["127.0.0.1"] and len(spool_files(n)) == 1  # bearer never left the host


# ---------------------------------------------------------------- bounds (N7, N8, N11)


def test_n7_spool_bounded_by_count_oldest_dropped(tmp_path: Path) -> None:
    n = make_notifier(tmp_path, lambda r: httpx.Response(500), spool_max=5)
    for i in range(8):
        n.publish(event(f"e{i}"))
    ids = [json.loads(f.read_text())["event_id"] for f in spool_files(n)]
    assert ids == [f"e{i}" for i in range(3, 8)]


def test_n7_spool_bounded_by_age(tmp_path: Path) -> None:
    clock = Clock()
    n = make_notifier(tmp_path, lambda r: httpx.Response(500), clock, spool_max_age=3600)
    n.publish(event("old"))
    for f in spool_files(n):
        os.utime(f, (clock.now - 7200, clock.now - 7200))
    n.publish(event("new"))
    assert [json.loads(f.read_text())["event_id"] for f in spool_files(n)] == ["new"]


def test_n8_url_empty_means_no_http_and_no_spool_file(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    assert load_config(settings.model_copy(update={"workbench_notify_url": ""})) is None
    assert build_notifier(settings) is None
    client = app_with(settings, workspace_config, None)
    assert post_gate(client, workspace_config, gate_body()).status_code == 200
    assert not (settings.log_dir / "workbench-notify-spool").exists()


async def test_n10_crash_after_the_response_before_sending_still_delivers_once(
    tmp_path: Path, settings: Settings, workspace_config: WorkspaceConfig
) -> None:
    n = make_notifier(tmp_path, lambda r: httpx.Response(202))
    post_gate(app_with(settings, workspace_config, n), workspace_config, gate_body())
    del n  # the sender never ran: the process "died"
    sent: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        return httpx.Response(202)

    revived = make_notifier(tmp_path, handler)
    await revived.send_pass()
    assert [b["event_id"] for b in sent] == ["c1"] and spool_files(revived) == []


def test_n11_oversized_comment_of_four_byte_characters_is_cut_to_the_byte_bound() -> None:
    text = "😀<&>\"'" * 2000  # four-byte characters and characters that JSON escapes
    ev = detect_gate(
        "c1", mention(INIT, text), [INIT], "a", INIT, "ws", ISSUE, "https://p.test", "proj"
    )
    assert ev is not None
    size = len(json.dumps(ev, ensure_ascii=False).encode())
    assert size <= MAX_REQUEST_BYTES and len(ev["text"]) <= 3500 and ev["text"].endswith("…")
    assert len(json.dumps(ev).encode()) < 8 * 1024 * 2  # still a bounded request
    assert (
        detect_gate(
            "c2", mention(INIT, "ok"), [INIT], "a", INIT, "ws", ISSUE, "http://p.test", "p"
        )["url"]
        is None
    )  # http url is omitted


def test_detector_rules() -> None:
    assert (
        detect_gate(None, mention(INIT), [INIT], "a", INIT, "ws", ISSUE, "https://p", "p") is None
    )
    assert (
        detect_gate("c", mention(INIT), [INIT], str(INIT), INIT, "ws", ISSUE, "https://p", "p")
        is None
    )
    assert detect_gate("c", mention(INIT), [], "a", INIT, "ws", ISSUE, "https://p", "p") is None
    other = UUID("11111111-1111-1111-1111-111111111111")
    assert (
        detect_gate("c", mention(other), [other, INIT], "a", INIT, "ws", ISSUE, "https://p", "p")
        is None
    )


# ---------------------------------------------------------------- URL rules and TLS (N9, N12-N14)


def test_n9_runner_spawn_env_has_no_tmux_and_marks_the_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
    e = spawn_env("ws")
    assert "TMUX" not in e and e["WORKBENCH_DRIVER"] == "runner" and e["WORKSPACE_SLUG"] == "ws"


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("http://127.0.0.1:8780", True),
        ("http://127.5.5.5:8780", True),
        ("http://[::1]:8780", True),
        ("https://wb.example.test", True),
        ("http://localhost:8780", False),  # a name, resolved: not loopback
        ("http://wb.example.test", False),
        ("http://203.0.113.9:8780", False),
        ("ftp://127.0.0.1", False),
        ("not a url", False),
    ],
)
def test_n13_url_validation_conductor_still_starts(
    settings: Settings, url: str, ok: bool, caplog: pytest.LogCaptureFixture
) -> None:
    s = settings.model_copy(update={"workbench_notify_url": url})
    got = load_config(s)
    assert (got is not None) == ok
    if got is not None:
        assert got.url == url.rstrip("/")


def test_n13_server_starts_with_a_refused_url(
    settings: Settings, workspaces: dict[str, WorkspaceConfig], tmp_path: Path
) -> None:
    from plane_conductor.server import create_app

    s = settings.model_copy(
        update={"workbench_notify_url": "http://wb.example.test", "log_dir": tmp_path}
    )
    app = create_app(s, workspaces)
    assert app.state.notifier is None


def test_loopback_literals_only() -> None:
    assert is_loopback_host("127.0.0.1") and is_loopback_host("::1") and is_loopback_host("[::1]")
    assert not is_loopback_host("localhost") and not is_loopback_host("10.0.0.1")
    assert not is_loopback_host("")


def test_n12_remote_https_url_is_accepted_and_the_call_goes_to_that_host(
    tmp_path: Path, settings: Settings
) -> None:
    s = settings.model_copy(
        update={
            "workbench_notify_url": "https://ingest.example.test",
            "workbench_notify_token_file": tmp_path / "token",
            "log_dir": tmp_path,
        }
    )
    (tmp_path / "token").write_text("tok-1")
    conf = load_config(s)
    assert conf is not None and conf.url == "https://ingest.example.test"
    assert conf.ca_file is None and conf.spool_dir == tmp_path / "workbench-notify-spool"
    hosts: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hosts.append((req.url.host, req.headers["authorization"]))
        return httpx.Response(202)

    n = Notifier(conf, transport=httpx.MockTransport(handler))
    n.publish(event())
    assert asyncio.run(n.send_pass()) == 1
    assert hosts == [("ingest.example.test", "Bearer tok-1")]


async def test_n12_real_tls_with_a_private_ca_file(tmp_path: Path) -> None:
    """The private CA is honoured; the same server is refused without it (N14, real handshake)."""
    trustme = pytest.importorskip("trustme")
    ca = trustme.CA()
    ca_file = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(str(ca_file))
    got: list[str] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        got.append(head.split(b"\r\n")[0].decode())
        length = int(
            next(x for x in head.split(b"\r\n") if x.lower().startswith(b"content-length")).split(
                b":"
            )[1]
        )
        await reader.readexactly(length)
        writer.write(b"HTTP/1.1 202 Accepted\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
        await writer.drain()
        writer.close()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ca.issue_cert("127.0.0.1").configure_cert(ctx)
    server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=ctx)
    port = server.sockets[0].getsockname()[1]
    try:
        url = f"https://127.0.0.1:{port}"
        trusted = Notifier(cfg(tmp_path, url=url, ca_file=ca_file), jitter=lambda: 0.0)
        trusted.publish(event("t"))
        assert await trusted.send_pass() == 1
        (tmp_path / "u").mkdir()
        untrusted = Notifier(cfg(tmp_path / "u", url=url), jitter=lambda: 0.0)  # system trust store
        untrusted.publish(event("u"))
        assert await untrusted.send_pass() == 0 and len(untrusted.files()) == 1
    finally:
        server.close()
        await server.wait_closed()
    assert got == ["POST /workbench/ingest/event HTTP/1.1"]


async def test_n14_untrusted_certificate_sends_nothing_keeps_the_file(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")

    n = make_notifier(tmp_path, handler, url="https://wb.example.test")
    n.publish(event())
    assert await n.send_pass() == 0 and len(spool_files(n)) == 1 and n.next_try > 0


async def test_unreadable_ca_file_keeps_the_file_and_retries(tmp_path: Path) -> None:
    n = Notifier(cfg(tmp_path, ca_file=tmp_path / "missing.pem"), jitter=lambda: 0.0)
    n.publish(event())
    assert await n.send_pass() == 0 and len(spool_files(n)) == 1 and n.next_try > 0


def test_client_always_verifies_and_never_follows_redirects(tmp_path: Path) -> None:
    n = Notifier(cfg(tmp_path, url="https://wb.example.test"))
    c = n._client()
    assert c.follow_redirects is False
    # no setting can turn verification off
    for field in Settings.model_fields:
        assert "verify" not in field and "insecure" not in field


# ---------------------------------------------------------------- outage and backoff (N15)


async def test_n15_long_outage_one_attempt_per_cycle_then_drains_oldest_first(
    tmp_path: Path,
) -> None:
    clock = Clock()
    attempts: list[str] = []
    state = {"down": True}
    delivered: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        eid = json.loads(req.content)["event_id"]
        if state["down"]:
            attempts.append(eid)
            raise httpx.ConnectError("unreachable")
        delivered.append(eid)
        return httpx.Response(202)

    n = make_notifier(tmp_path, handler, clock)
    for i in range(20):
        n.publish(event(f"e{i:02d}"))
    waits = []
    for _ in range(8):  # eight cycles of a simulated multi-hour outage
        before = len(attempts)
        await n.send_pass()
        assert len(attempts) - before == 1  # one attempt per cycle, whatever the spool size
        waits.append(n.next_try - clock.now)
        clock.now = n.next_try
    assert waits[0] == BACKOFF_START and waits[1] == BACKOFF_START * 2
    assert max(waits) == BACKOFF_CAP and waits[-1] == BACKOFF_CAP
    assert len(spool_files(n)) == 20
    state["down"] = False  # recovery: the spool drains oldest-first, each event once
    await n.send_pass()
    assert delivered == [f"e{i:02d}" for i in range(20)] and spool_files(n) == []
    assert n.backoff == BACKOFF_START


async def test_error_log_for_a_repeating_failure_at_most_once_per_ten_minutes(
    tmp_path: Path, capture_logs: Recorder
) -> None:
    clock = Clock()
    n = make_notifier(tmp_path, lambda r: httpx.Response(401), clock)
    n.publish(event())
    for _ in range(5):
        await n.send_pass()
        clock.now = n.next_try
    logs = capture_logs.records
    failing = [r for r in logs if r["event"] == "workbench_notify_failing"]
    assert len(failing) == 1 and failing[0]["reason"] == "http_401"
    assert "tok-1" not in json.dumps(logs, default=str)


def test_token_file_is_reread_on_every_send(tmp_path: Path) -> None:
    n = make_notifier(tmp_path, lambda r: httpx.Response(202))
    assert n._token() == "tok-1"
    (tmp_path / "token").write_text("tok-2\n")
    assert n._token() == "tok-2"
    (tmp_path / "token").unlink()
    assert n._token() is None  # missing file: the call is made without a bearer -> 401 -> kept


def test_gate_text_decodes_html_entities_and_spool_is_utf8(tmp_path: Path) -> None:
    ev = detect_gate(
        "c3",
        mention(INIT, "Tom &amp; Jerry &lt;ok&gt; привет"),
        [INIT],
        "a",
        INIT,
        "ws",
        ISSUE,
        "https://p.test",
        "proj",
    )
    assert ev is not None and "Tom & Jerry <ok> привет" in ev["text"]
    assert "&amp;" not in ev["text"]
