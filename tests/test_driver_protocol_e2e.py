"""子进程端到端协议测试：真实 `python -m roost.driver` + 真实 localhost HTTP。

防的回归（CONTRACTS.md 附录 B 的验收面）：

- 重复 POST 同一 turn_id **恰好执行一次**（I1 的 driver 侧）；
- 事件长轮询的 cursor 续读不丢不重；
- harness 抛异常时有 `Terminal(status="error")` 兜底——turn 永远有终态；
- `/v1/health` 就绪、协议版本门、`/v1/update` 的 501 占位。

刻意起真实进程而不是在测试进程内跑 server：driver 的存在理由就是"在别的进程/
沙箱里"，进程边界上的问题（模块入口、端口绑定、就绪信号、报文层）只有这样才暴露。
宿主侧全程走 `ControlClient` + `SandboxBackend.request` 通道，等于同时验了 host 侧客户端。
"""

from __future__ import annotations

import asyncio
import http.client
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator

import pytest

from roost import ControlClient, SandboxHandle, TurnEnvelope, UnknownTurnError
from roost.events import Delta, Terminal

READY_TIMEOUT = 30.0
DRAIN_TIMEOUT = 20.0
HANDLE = SandboxHandle(sandbox_id="local-driver", backend="subprocess")


class LoopbackBackend:
    """只实现 `request` 的 SandboxBackend：把控制面请求打到 127.0.0.1:port。

    其余方法在 M2 无被调用路径，故意 NotImplementedError——若哪天 ControlClient
    偷偷用了别的能力，测试会立刻炸而不是悄悄通过。
    """

    def __init__(self, port: int) -> None:
        self._port = port

    async def request(
        self,
        handle: SandboxHandle,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, bytes]:
        del handle
        return await asyncio.to_thread(
            self._request_sync, method, path, body, headers or {}, timeout_seconds
        )

    def _request_sync(
        self,
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout_seconds: float | None,
    ) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection(
            "127.0.0.1", self._port, timeout=timeout_seconds
        )
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def __getattr__(self, name: str):        # create / connect / exec / ...
        raise NotImplementedError(f"LoopbackBackend 不提供 {name}")


class DriverProcess:
    def __init__(self, proc: subprocess.Popen[str], port: int) -> None:
        self.proc = proc
        self.port = port
        self.backend = LoopbackBackend(port)

    def client(self, **kwargs) -> ControlClient:
        return ControlClient(self.backend, HANDLE, **kwargs)


@pytest.fixture
async def driver() -> AsyncIterator[DriverProcess]:
    """起一个真实 driver 进程；端口由内核分配，就绪行从 stdout 读。"""
    env = {**os.environ, "ROOST_DRIVER_PORT": "0", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "roost.driver"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert proc.stdout is not None
        try:
            line = await asyncio.wait_for(
                asyncio.to_thread(proc.stdout.readline), timeout=READY_TIMEOUT
            )
        except TimeoutError:
            raise AssertionError("driver 未在超时内打印就绪行") from None
        if not line:
            raise AssertionError(f"driver 提前退出（returncode={proc.poll()}）")
        assert "listening on 127.0.0.1:" in line, line
        yield DriverProcess(proc, int(line.rsplit(":", 1)[1]))
    finally:
        # 无论断言是否失败都不留孤儿进程：先 SIGTERM，宽限后 SIGKILL。
        proc.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=10)
        except TimeoutError:
            proc.kill()
            await asyncio.to_thread(proc.wait)
        if proc.stdout is not None:
            proc.stdout.close()


def make_turn(turn_id: str, **payload) -> TurnEnvelope:
    return TurnEnvelope(turn_id=turn_id, session_id="s-1", payload=payload)


async def drain(client: ControlClient, turn_id: str) -> list:
    """按 cursor 续读到 Terminal 为止，返回全部事件。"""
    events: list = []
    after = 0
    deadline = time.monotonic() + DRAIN_TIMEOUT
    while time.monotonic() < deadline:
        page = await client.fetch_events(turn_id, after=after, wait_ms=1000)
        assert page.next_after >= after
        events.extend(page.events)
        after = page.next_after
        if events and isinstance(events[-1], Terminal):
            return events
    raise AssertionError(f"turn {turn_id!r} 在 {DRAIN_TIMEOUT}s 内没有终态")


async def test_health_reports_ready_driver(driver: DriverProcess) -> None:
    health = await driver.client().health()

    assert health.ok is True
    assert health.protocol_version == "1"
    assert health.harness_ready is True
    assert health.uptime_ms >= 0


async def test_duplicate_submits_execute_exactly_once(driver: DriverProcess) -> None:
    client = driver.client()
    turn = make_turn("t-dup", text="abcdefghij", delay_ms=20)

    # 并发四份同一 envelope（at-least-once 投递的现实形状）。
    results = await asyncio.gather(*(client.submit_turn(turn) for _ in range(4)))

    accepted = [r for r in results if not r.is_duplicate]
    assert len(accepted) == 1, [r.state for r in results]
    assert all(r.turn_id == "t-dup" for r in results)

    events = await drain(client, "t-dup")
    deltas = [e for e in events if isinstance(e, Delta)]
    assert "".join(d.text for d in deltas) == "abcdefghij"   # 恰好回显一次
    assert [e.seq for e in events] == list(range(1, len(events) + 1))

    # 终态后再次提交：仍然 duplicate，仍然不重跑（事件序列一字不变）。
    again = await client.submit_turn(turn)
    assert (again.is_duplicate, again.turn_state) == (True, "done")
    assert await drain(client, "t-dup") == events


async def test_long_poll_cursor_resumes_without_gaps_or_repeats(
    driver: DriverProcess,
) -> None:
    client = driver.client()
    turn = make_turn("t-cursor", text="x" * 64, delay_ms=15)
    await client.submit_turn(turn)

    collected: list = []
    after = 0
    rounds = 0
    deadline = time.monotonic() + DRAIN_TIMEOUT
    while time.monotonic() < deadline:
        page = await client.fetch_events("t-cursor", after=after, wait_ms=200)
        rounds += 1
        if page.events:
            # 同一 cursor 重复读是幂等的：再读一次拿到完全相同的一页。
            assert await client.fetch_events(
                "t-cursor", after=after, wait_ms=0
            ) == page
            collected.extend(page.events)
            after = page.next_after
        if collected and isinstance(collected[-1], Terminal):
            break
    else:
        raise AssertionError("cursor 续读未在超时内看到终态")

    assert rounds > 1, "delay_ms 下应当发生多轮续读"
    assert [e.seq for e in collected] == list(range(1, len(collected) + 1))
    assert "".join(e.text for e in collected if isinstance(e, Delta)) == "x" * 64

    # 终态之后的等待：空页，cursor 不动（长轮询超时是正常返回，不是错误）。
    started = time.monotonic()
    page = await client.fetch_events("t-cursor", after=after, wait_ms=300)
    assert (page.events, page.next_after) == ([], after)
    assert time.monotonic() - started >= 0.25


async def test_harness_exception_yields_terminal_error(driver: DriverProcess) -> None:
    client = driver.client()
    await client.submit_turn(make_turn("t-boom", text="partial", fail="boom"))

    events = await drain(client, "t-boom")
    terminal = events[-1]

    assert isinstance(terminal, Terminal)
    assert terminal.status == "error"
    assert "boom" in (terminal.error or "")
    assert terminal.seq == len(events)


async def test_missing_terminal_is_backfilled_by_driver(
    driver: DriverProcess,
) -> None:
    """harness 正常返回却没 emit Terminal —— "Terminal 恒为最后一条"仍成立。"""
    client = driver.client()
    await client.submit_turn(make_turn("t-silent", text="hi", skip_terminal=True))

    terminal = (await drain(client, "t-silent"))[-1]

    assert isinstance(terminal, Terminal)
    assert terminal.status == "error"


async def test_shutdown_is_prompt_with_inflight_long_poll(
    driver: DriverProcess,
) -> None:
    """SIGTERM 时挂着的长轮询不得拖住关停。

    防的回归（M2 实现期真实踩到）：asyncio 自带的 `serve_forever` 取消收尾会
    `wait_closed()`，它等的是在飞连接自然结束——一个 30s 的长轮询就能把关停拖满
    30s。沙箱替换、forced update（M6）、watchdog 强杀都踩在这条路上，慢关停会被
    误判成 hang。
    """
    client = driver.client()
    await client.submit_turn(make_turn("t-shutdown", text="hi"))
    poll = asyncio.create_task(
        client.fetch_events("t-shutdown", after=99, wait_ms=30_000)
    )
    await asyncio.sleep(0.3)

    started = time.monotonic()
    driver.proc.terminate()
    returncode = await asyncio.wait_for(
        asyncio.to_thread(driver.proc.wait), timeout=15
    )
    elapsed = time.monotonic() - started

    poll.cancel()
    await asyncio.gather(poll, return_exceptions=True)
    assert returncode == 0
    assert elapsed < 5, f"关停耗时 {elapsed:.1f}s"


async def test_unknown_turn_is_404(driver: DriverProcess) -> None:
    with pytest.raises(UnknownTurnError):
        await driver.client().fetch_events("never-submitted", wait_ms=0)


async def test_protocol_version_gate_and_reserved_endpoints(
    driver: DriverProcess,
) -> None:
    backend = driver.backend

    status, body = await backend.request(
        HANDLE, "GET", "/v1/health", headers={"X-Roost-Protocol-Version": "99"}
    )
    assert (status, b"unsupported_protocol_version" in body) == (400, True)

    status, _ = await backend.request(HANDLE, "GET", "/v1/health", headers={})
    assert status == 400, "缺失协议版本 header 与版本不识别同等对待"

    status, body = await backend.request(
        HANDLE, "POST", "/v1/update", headers={"X-Roost-Protocol-Version": "1"}
    )
    assert (status, b"reserved_until_m6" in body) == (501, True)

    status, body = await backend.request(
        HANDLE,
        "POST",
        "/v1/turn",
        body=b"{not json}",
        headers={"X-Roost-Protocol-Version": "1"},
    )
    assert (status, b"invalid_body" in body) == (400, True)
