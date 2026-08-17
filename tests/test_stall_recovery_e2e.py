"""M12 端到端：升级阶梯在真 docker 上一级一级走完（CONTRACTS.md 附录 M）。

防的回归（附录 H / M、DESIGN.md I1、I3）：

- **卡死不再是死局，而且不是一步到位**：harness 一个事件都不发地挂住 →
  双时钟判死 → 阶梯第一级**重启沙箱内的 driver 进程**（容器与工作区都留着）→
  仍然挂死 → 第二级杀沙箱 → pipeline 放手（不收尾）→ 锁自然过期 → watchdog
  sweep → attempt+1 重投 → cold boot 的新沙箱把答案给出来。这条链上任何一环
  断掉，用户的消息就永久没有答复，而且没有任何东西会报错。
- **恢复只有一条路径**：`hang_on_attempt=1` 的 turn 在 attempt=1 上被执行两次
  （原进程一次、restart 后一次），attempt=2 答复一次。投递层的失败重投若也在推
  同一个 turn，次数会对不上。
- **慢而活不被杀**：liveness 竭（4 秒不出事件）、progress 未竭、活性探测在真
  `/proc` 上看到一个在等 IO 的后代进程 → silence-defer 并继续。这是矩阵第三行
  的实机验证，也是"慢"和"死"不被混为一谈的那条线。
- **观测纪律**：重复 defer 不重复写。

刻意全程走 delivery → TurnProcessor → SandboxTurnRunner → Watchdog 这条真实链路：
判活在 runner、放手在 pipeline、重投在 watchdog，三者的接缝正是要验的东西。
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

# 阈值刻意调到秒级，好让一次真实的 hang → restart → kill → 恢复在测试时间尺度内
# 走完。三者互不耦合正是契约要求的：锁由心跳维持，双时钟只看 driver 的活动与
# 可渲染事件，watchdog interval 只决定发现得多快。
BOOT_GRACE = 4.0
LIVENESS_QUIET = 3.0
PROGRESS_QUIET = 60.0
FIRST_RENDERABLE = 60.0
LOCK_SECONDS = 2
WATCHDOG_INTERVAL = 0.5
WAIT_MS = 1_000
RECOVERY_TIMEOUT = 420.0


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
    """M1 内核 + M3b 编排 + M5 watchdog + M12 判活接成一套可跑的宿主。"""

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
            store=self.store,
            ops=self.ops,
            wait_ms=WAIT_MS,
            boot_grace=BOOT_GRACE,
            liveness_quiet=LIVENESS_QUIET,
            progress_quiet=PROGRESS_QUIET,
            first_renderable=FIRST_RENDERABLE,
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

    def turn_row(self, turn_id: str) -> tuple[str, int] | None:
        """直接读 source of truth（port 面不暴露它，e2e 断言终态与阶梯需要）。"""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT status, error_ordinal FROM roost_turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None    # 建表是懒的：第一个 turn 落库之前表还不存在
        finally:
            conn.close()
        return None if row is None else (row[0], row[1])

    def turn_status(self, turn_id: str) -> str | None:
        row = self.turn_row(turn_id)
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


async def test_hung_turn_climbs_the_ladder_and_is_answered_by_a_new_sandbox(
    host: Host,
) -> None:
    """挂死 → 重启 driver（沙箱不动）→ 仍挂死 → 杀沙箱 → requeue → 新沙箱答复。"""
    session = f"s-{uuid.uuid4().hex[:8]}"
    turn_id = "turn-hang"

    await host.delivery.enqueue(
        TurnEnvelope(
            turn_id=turn_id,
            session_id=session,
            payload={"text": "recovered", "hang_on_attempt": 1},
        )
    )
    await host.wait_for_status(turn_id, "finished", RECOVERY_TIMEOUT)

    # 1) 阶梯第一级：driver 进程被重启过恰好一次，且**没有**动沙箱。
    restarts = host.ops.all_of("driver_restarted")
    assert len(restarts) == 1
    restarted_sandbox = restarts[0]["sandbox_id"]
    assert host.ops.all_of("driver_restart_requested")[0]["error_ordinal"] == 1

    # 2) 阶梯第二级：同一个沙箱在第二次判死时被杀（ordinal=2）。
    killed = host.ops.all_of("sandbox_stalled_killed")
    assert len(killed) == 1
    assert killed[0]["turn_id"] == turn_id
    assert killed[0]["sandbox_id"] == restarted_sandbox
    assert killed[0]["error_ordinal"] == 2
    # 判定字段齐全（附录 M 的观测面）：谁触的线、两个时钟当时各是多少。
    assert killed[0]["threshold_tripped"] in {"liveness", "progress"}
    assert killed[0]["liveness_quiet_ms"] >= LIVENESS_QUIET * 1000
    assert killed[0]["turn_age_ms"] > 0

    # 3) watchdog 重投过这个 turn（attempt 由投递方 +1）。
    requeued = host.ops.all_of("watchdog_requeued")
    assert [turn_id] in [details["turn_ids"] for details in requeued]

    # 4) 提交恰好三次：原进程、restart 后的新进程、新沙箱。三次都是 accepted——
    #    restart 后的那次之所以合法，正是因为新进程的 registry 是空的。
    submissions = host.ops.all_of("turn_submitted")
    assert [s["state"] for s in submissions] == ["accepted"] * 3
    assert [s["after_restart"] for s in submissions] == [False, True, False]

    # 5) 旧沙箱已销毁；新沙箱还活着，且换了一个。
    second = await host.store.get_binding(session)
    assert second is not None and second.sandbox_id != restarted_sandbox
    assert not _container_exists(restarted_sandbox)
    assert _container_exists(second.sandbox_id)

    # 6) 答案恰好给出一次，行终态 finished，阶梯计数留在库里（不随重投复位）。
    assert host.sink.text() == "recovered"
    assert host.turn_row(turn_id) == ("finished", 2)

    # 7) 终态之后不再被扫出来：sweep 不会把一个已完成的 turn 反复重投。
    await asyncio.sleep(LOCK_SECONDS + WATCHDOG_INTERVAL * 2)
    assert host.ops.all_of("watchdog_requeued") == requeued
    assert len(host.ops.all_of("turn_submitted")) == 3


async def test_slow_but_active_turn_is_deferred_not_killed(host: Host) -> None:
    """矩阵第三行的实机验证：liveness 竭、progress 未竭、/proc 说 ACTIVE → 继续。

    `busy_child` 让沙箱里真的有一个在等 IO 的后代进程——这正是慢 API 调用/长 tool
    在内核眼里的样子。少了它，"慢"和"死"在 /proc 上无从分辨。
    """
    session = f"s-{uuid.uuid4().hex[:8]}"
    turn_id = "turn-slow"

    await host.delivery.enqueue(
        TurnEnvelope(
            turn_id=turn_id,
            session_id=session,
            payload={
                "text": "slowbutalive",
                "delay_ms": 4_000,      # > liveness_quiet，< progress_quiet
                "busy_child": True,
            },
        )
    )
    await host.wait_for_status(turn_id, "finished", RECOVERY_TIMEOUT)

    assert host.sink.text() == "slowbutalive"
    # 没有任何一级阶梯被走过。
    assert host.ops.all_of("sandbox_stalled_killed") == []
    assert host.ops.all_of("driver_restarted") == []
    assert host.ops.all_of("watchdog_requeued") == []
    assert len(host.ops.all_of("turn_submitted")) == 1
    assert host.turn_row(turn_id) == ("finished", 0)   # 阶梯计数一次都没动

    # defer 确实发生过（说明判定真的走到了矩阵第三行），且**只写了一条**。
    deferred = host.ops.all_of("watch_silence_deferred")
    assert len(deferred) == 1
    assert deferred[0]["probe_active"] is True
    assert deferred[0]["silence_deferred_count"] == 1

    # sweep 确实跑过（不是 watchdog 没起来），且每一次都空转。
    assert len(host.store.sweeps) >= 2
    assert set(host.store.sweeps) == {0}
    # 沙箱自始至终是同一个，没有被换掉。
    binding = await host.store.get_binding(session)
    assert binding is not None and _container_exists(binding.sandbox_id)
