"""M5 端到端：卡死的沙箱被杀、turn 被 requeue、答案在新沙箱里给出（真 docker）。

防的回归（CONTRACTS.md 附录 H / DESIGN.md I1、I3）：

- **卡死不再是死局**：harness 一个事件都不发地挂住 → 宿主侧停滞检测销毁沙箱 →
  pipeline 放手（不收尾）→ 锁自然过期 → watchdog sweep → attempt+1 重投 →
  cold boot 的新沙箱把答案给出来。这条链上任何一环断掉，用户的消息就永久没有
  答复，而且没有任何东西会报错——这是本库最贵的一类静默失败。
- **恢复只有这一条路径**：`hang_on_attempt=1` 的 turn 恰好执行两次（第一次挂死、
  第二次答复）。执行三次意味着投递层的失败重投也在推同一个 turn，即两条恢复
  路径并存（I1 排除的形态）；只执行一次意味着重投的 turn 被当成 duplicate 吃掉。
- **idle 不误杀**：事件间隔逼近但不超过 stall_timeout 的慢 turn 必须跑完，
  且 watchdog 全程空转——否则 M5 就把"慢"和"死"混为一谈，正常的长任务会被腰斩。

刻意全程走 delivery → TurnProcessor → SandboxTurnRunner → Watchdog 这条真实链路：
停滞判定在 runner、放手在 pipeline、重投在 watchdog，三者的接缝正是要验的东西。
本机无 docker 时整文件 skip；夹具兜底清理本次新建的容器（roost.sandbox=1 label）。
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import uuid
from pathlib import Path

import pytest

from roost import (
    DisplayEvent,
    DockerSandboxBackend,
    InProcessTurnDelivery,
    SandboxTurnRunner,
    SessionSandboxRegistry,
    SQLiteStateStore,
    TurnEnvelope,
    TurnProcessor,
    Watchdog,
)
from roost.backends import SANDBOX_LABEL

IMAGE = "python:3.12-slim"
BOOT_TIMEOUT = 180.0

# 停滞判定与锁都刻意调短，好让一次真实的 hang → 恢复在测试时间尺度内走完。
# 三者互不耦合正是契约要求的：锁由心跳维持（stall 之前不会过期），
# stall_timeout 只看事件间隔，watchdog interval 只决定发现得多快。
STALL_TIMEOUT = 6.0
LOCK_SECONDS = 2
WATCHDOG_INTERVAL = 0.5
WAIT_MS = 1_000
RECOVERY_TIMEOUT = 300.0


def _docker_available() -> bool:
    try:
        done = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="docker daemon unavailable"
)


def _labelled_containers() -> set[str]:
    done = subprocess.run(
        ["docker", "ps", "-aq", "--no-trunc", "--filter", f"label={SANDBOX_LABEL}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return {line.strip() for line in done.stdout.splitlines() if line.strip()}


def _container_exists(sandbox_id: str) -> bool:
    done = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", sandbox_id],
        capture_output=True,
        timeout=30,
    )
    return done.returncode == 0


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[DisplayEvent] = []

    async def emit(self, events: list[DisplayEvent]) -> None:
        self.events.extend(events)

    def of_kind(self, kind: str) -> list[DisplayEvent]:
        return [event for event in self.events if event.kind == kind]

    def text(self) -> str:
        return "".join(event.body["text"] for event in self.of_kind("text"))


class RecordingOps:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type: str, /, **details: object) -> None:
        self.events.append((event_type, dict(details)))

    def kinds(self) -> list[str]:
        return [event for event, _ in self.events]

    def all_of(self, event_type: str) -> list[dict]:
        return [details for name, details in self.events if name == event_type]


class CountingStore:
    """在真 StateStore 外面数一层 sweep：既要断言"扫过"，也要断言"扫出来是空的"。"""

    def __init__(self, inner: SQLiteStateStore) -> None:
        self._inner = inner
        self.sweeps: list[int] = []   # 每次 sweep 返回的行数

    async def sweep_due_turns(self, *, limit: int):
        swept = await self._inner.sweep_due_turns(limit=limit)
        self.sweeps.append(len(swept))
        return swept

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


@pytest.fixture
def sandbox_cleanup():
    before = _labelled_containers()
    try:
        yield
    finally:
        for sandbox_id in _labelled_containers() - before:
            subprocess.run(
                ["docker", "rm", "-f", sandbox_id], capture_output=True, timeout=120
            )


class Host:
    """M1 内核 + M3b 编排 + M5 watchdog 接成一套可跑的宿主（测试用最小装配）。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.backend = DockerSandboxBackend(image=IMAGE)
        self.inner_store = SQLiteStateStore(db_path)
        self.store = CountingStore(self.inner_store)
        self.sink = RecordingSink()
        self.ops = RecordingOps()
        self.registry = SessionSandboxRegistry(
            self.backend,
            self.store,
            sink=self.sink,
            ops=self.ops,
            template=IMAGE,
            boot_timeout=BOOT_TIMEOUT,
        )
        self.runner = SandboxTurnRunner(
            self.registry,
            self.sink,
            ops=self.ops,
            wait_ms=WAIT_MS,
            stall_timeout=STALL_TIMEOUT,
        )
        self.delivery = InProcessTurnDelivery()
        self.processor = TurnProcessor(
            self.store, self.runner, delivery=self.delivery, lock_seconds=LOCK_SECONDS
        )
        self.delivery.start(self.processor.process)
        self.watchdog = Watchdog(
            self.store, self.delivery, interval=WATCHDOG_INTERVAL, ops=self.ops
        )
        self.watchdog.start()

    def turn_status(self, turn_id: str) -> str | None:
        """直接读 source of truth 的行状态（port 面不暴露它，e2e 断言终态需要）。"""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT status FROM roost_turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            return None    # 建表是懒的：第一个 turn 落库之前表还不存在
        finally:
            conn.close()
        return None if row is None else row[0]

    async def wait_for_status(self, turn_id: str, status: str, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.turn_status(turn_id) == status:
                return
            await asyncio.sleep(0.2)
        raise AssertionError(
            f"turn {turn_id!r} 在 {timeout}s 内没有到达 {status!r}，"
            f"当前 {self.turn_status(turn_id)!r}；ops={self.ops.kinds()}"
        )

    async def close(self) -> None:
        await self.watchdog.stop()
        await self.delivery.stop()
        await self.inner_store.close()


@pytest.fixture
async def host(sandbox_cleanup, tmp_path: Path):
    harness = Host(tmp_path / "roost.db")
    try:
        yield harness
    finally:
        await harness.close()


async def test_hung_turn_is_killed_requeued_and_answered_by_a_new_sandbox(
    host: Host,
) -> None:
    """attempt=1 挂死 → 沙箱被杀 → watchdog requeue → attempt=2 在新沙箱答复。"""
    session = f"s-{uuid.uuid4().hex[:8]}"
    turn_id = "turn-hang"

    await host.delivery.enqueue(
        TurnEnvelope(
            turn_id=turn_id,
            session_id=session,
            payload={"text": "recovered", "hang_on_attempt": 1},
        )
    )

    # 第一个沙箱：turn 在里面挂住，等它被停滞判定杀掉。
    await host.wait_for_status(turn_id, "finished", RECOVERY_TIMEOUT)

    # 1) 恰好杀过一次，且杀的是第一个沙箱。
    killed = host.ops.all_of("sandbox_stalled_killed")
    assert len(killed) == 1
    assert killed[0]["turn_id"] == turn_id
    first_sandbox = killed[0]["sandbox_id"]

    # 2) watchdog 重投过这个 turn（attempt 由投递方 +1）。
    requeued = host.ops.all_of("watchdog_requeued")
    assert [turn_id] in [details["turn_ids"] for details in requeued]

    # 3) 恰好执行两次：两次提交都是 accepted（duplicate 不会重跑），
    #    并且第二次跑在**另一个**沙箱里。
    submissions = host.ops.all_of("turn_submitted")
    assert [s["state"] for s in submissions] == ["accepted", "accepted"]
    second = await host.store.get_binding(session)
    assert second is not None and second.sandbox_id != first_sandbox

    # 4) 旧沙箱已销毁；新沙箱还活着。
    assert not _container_exists(first_sandbox)
    assert _container_exists(second.sandbox_id)

    # 5) 答案恰好给出一次，行终态 finished。
    assert host.sink.text() == "recovered"
    assert host.turn_status(turn_id) == "finished"

    # 6) 终态之后不再被扫出来：sweep 不会把一个已完成的 turn 反复重投。
    await asyncio.sleep(LOCK_SECONDS + WATCHDOG_INTERVAL * 2)
    assert host.ops.all_of("watchdog_requeued") == requeued
    assert len(host.ops.all_of("turn_submitted")) == 2


async def test_slow_turn_is_not_mistaken_for_a_hang(host: Host) -> None:
    """事件间隔逼近但不超 stall_timeout 的慢 turn：正常完成，sweep 全程空转。"""
    session = f"s-{uuid.uuid4().hex[:8]}"
    turn_id = "turn-slow"

    # 每个 Delta 之前等 delay_ms；间隔 4s < stall_timeout 6s，且总时长远超它——
    # "总时长上限"式的判定会在这里误杀，"事件间隔"式的判定不会。
    await host.delivery.enqueue(
        TurnEnvelope(
            turn_id=turn_id,
            session_id=session,
            payload={"text": "slowbutalive", "delay_ms": 4_000},
        )
    )
    await host.wait_for_status(turn_id, "finished", RECOVERY_TIMEOUT)

    assert host.sink.text() == "slowbutalive"
    assert host.ops.all_of("sandbox_stalled_killed") == []
    assert host.ops.all_of("watchdog_requeued") == []
    assert len(host.ops.all_of("turn_submitted")) == 1
    # sweep 确实跑过（不是 watchdog 没起来），且每一次都空转。
    assert len(host.store.sweeps) >= 2
    assert set(host.store.sweeps) == {0}
    # 沙箱自始至终是同一个，没有被换掉。
    binding = await host.store.get_binding(session)
    assert binding is not None and _container_exists(binding.sandbox_id)
