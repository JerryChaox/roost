"""M3b 编排端到端：真 docker 容器 + 真 driver 进程 + 真 HTTP 控制面。

防的回归（附录 F 的验收面）——这是 **Demo 1 的自动化形态**：

- cold boot 一个真沙箱、把 driver 装进去、跑完一个 turn，DisplayEvent 流以
  Terminal 收尾且 seq 严格递增；
- 同一个 turn_id 经 at-least-once 投递双投，**只执行一次、只有一份终态**
  （I1 的 host 侧 + driver 侧合起来生效）；
- 连续两个 turn 复用同一沙箱（不每轮重建）；
- 沙箱被 `docker rm -f` 之后，下一个 turn 自动 cold boot 新沙箱并换绑。

刻意全程走 delivery → TurnProcessor → SandboxTurnRunner 这条真实链路，而不是直接
调 runner：幂等是"投递层重复 + 状态层 CAS + driver registry"三段合起来的性质，
只测中间一段证明不了它。本机无 docker 时整文件 skip；夹具兜底清理本次新建的
容器（全部带 roost.sandbox=1 label）。
"""

from __future__ import annotations

import subprocess
import uuid

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
)
from roost.backends import SANDBOX_LABEL

IMAGE = "python:3.12-slim"
BOOT_TIMEOUT = 180.0


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
    """EventSink port 的记录实现。"""

    def __init__(self) -> None:
        self.events: list[DisplayEvent] = []

    async def emit(self, events: list[DisplayEvent]) -> None:
        self.events.extend(events)

    def of_kind(self, kind: str) -> list[DisplayEvent]:
        return [event for event in self.events if event.kind == kind]

    def text(self) -> str:
        return "".join(event.body["text"] for event in self.of_kind("text"))


@pytest.fixture
def sandbox_cleanup():
    """兜底清理：移除本次测试新建的、带 roost.sandbox=1 label 的容器。"""
    before = _labelled_containers()
    try:
        yield
    finally:
        for sandbox_id in _labelled_containers() - before:
            subprocess.run(
                ["docker", "rm", "-f", sandbox_id], capture_output=True, timeout=120
            )


class Harness:
    """把 M1 内核 + M3b 编排接成一套可跑的宿主（测试用最小装配）。"""

    def __init__(self, *, duplicate: bool) -> None:
        self.backend = DockerSandboxBackend(image=IMAGE)
        self.store = SQLiteStateStore(None)
        self.sink = RecordingSink()
        self.registry = SessionSandboxRegistry(
            self.backend,
            self.store,
            sink=self.sink,
            template=IMAGE,
            boot_timeout=BOOT_TIMEOUT,
        )
        self.runner = SandboxTurnRunner(self.registry, self.sink)
        self.delivery = InProcessTurnDelivery(duplicate_factor=2 if duplicate else 1)
        self.processor = TurnProcessor(self.store, self.runner, delivery=self.delivery)
        self.delivery.start(self.processor.process)

    async def say(self, session_id: str, turn_id: str, text: str) -> None:
        await self.delivery.enqueue(
            TurnEnvelope(turn_id=turn_id, session_id=session_id, payload={"text": text})
        )
        await self.delivery.join()

    async def close(self) -> None:
        await self.delivery.stop()
        await self.store.close()


@pytest.fixture
async def host(sandbox_cleanup):
    harness = Harness(duplicate=False)
    try:
        yield harness
    finally:
        await harness.close()


@pytest.fixture
async def duplicating_host(sandbox_cleanup):
    harness = Harness(duplicate=True)
    try:
        yield harness
    finally:
        await harness.close()


async def test_cold_boot_then_turn_streams_display_events(host) -> None:
    session = f"s-{uuid.uuid4().hex[:8]}"

    await host.say(session, "turn-1", "hello roost")

    assert host.sink.text() == "hello roost"
    terminals = host.sink.of_kind("terminal")
    assert len(terminals) == 1
    assert terminals[0].body["status"] == "ok"
    assert terminals[0] is host.sink.events[-1]

    kinds = [event.body["kind"] for event in host.sink.of_kind("lifecycle_notice")]
    assert kinds == ["boot_started", "boot_finished"]

    seqs = [event.seq for event in host.sink.events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert {event.session_id for event in host.sink.events} == {session}

    binding = await host.store.get_binding(session)
    assert binding is not None and binding.backend == "docker"


async def test_duplicate_delivery_executes_turn_exactly_once(duplicating_host) -> None:
    """Demo 1：投递两份，agent 只答一次。"""
    session = f"s-{uuid.uuid4().hex[:8]}"

    await duplicating_host.say(session, "turn-dup", "只答一次")

    assert duplicating_host.sink.text() == "只答一次"
    assert len(duplicating_host.sink.of_kind("terminal")) == 1
    assert not duplicating_host.delivery.dropped


async def test_consecutive_turns_reuse_same_sandbox(host) -> None:
    session = f"s-{uuid.uuid4().hex[:8]}"

    await host.say(session, "turn-1", "first")
    first = await host.store.get_binding(session)
    await host.say(session, "turn-2", "second")
    second = await host.store.get_binding(session)

    assert first == second
    assert host.sink.text() == "firstsecond"
    # 复用路径不再 boot，因此 lifecycle 通告仍然只有第一轮那两条。
    assert len(host.sink.of_kind("lifecycle_notice")) == 2
    assert len(host.sink.of_kind("terminal")) == 2


async def test_killed_sandbox_is_replaced_on_next_turn(host) -> None:
    session = f"s-{uuid.uuid4().hex[:8]}"
    await host.say(session, "turn-1", "before")
    first = await host.store.get_binding(session)
    assert first is not None

    subprocess.run(
        ["docker", "rm", "-f", first.sandbox_id], capture_output=True, timeout=120
    )
    assert not _container_exists(first.sandbox_id)

    await host.say(session, "turn-2", "after")

    second = await host.store.get_binding(session)
    assert second is not None and second.sandbox_id != first.sandbox_id
    assert _container_exists(second.sandbox_id)
    assert host.sink.text() == "beforeafter"
    assert len(host.sink.of_kind("lifecycle_notice")) == 4     # 两次 cold boot
