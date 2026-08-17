"""Demo 2 端到端：工作区跨沙箱死亡而存活（真 docker + 真 driver + 真快照文件）。

防的回归（CONTRACTS.md 附录 G 的验收面 / DESIGN.md I2）：

- counter turn 在沙箱里写工作区（=1）→ turn 边界的备份把工作区写进 SnapshotStore
  → `docker rm -f` 掉沙箱 → 下一个 turn 自动 cold boot 新沙箱、恢复工作区，
  counter **续增到 2**。counter 是最小的"状态确实回来了"的观测面：它读的是文件，
  不是内存，也不是宿主这边任何一份影子状态。
- **恢复失败按 boot 失败处理**：注入一份损坏 snapshot → cold boot raise，且不留
  活容器。这条比成功路径更值得测——一个恢复失败却照常交出去的空沙箱会在下一个
  turn 边界把空工作区备份回去，真状态就此永久丢失。

刻意全程走 delivery → TurnProcessor → SandboxTurnRunner 这条真实链路（与 M3b 的
编排 e2e 同构）：备份是挂在 runner 的终态收尾上的，只调 registry 证明不了它。
本机无 docker 时整文件 skip；夹具兜底清理本次新建的容器（roost.sandbox=1 label）。
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from roost import (
    BackupCoordinator,
    ControlError,
    DisplayEvent,
    DockerSandboxBackend,
    FileSnapshotStore,
    InProcessTurnDelivery,
    SandboxTurnRunner,
    SessionSandboxRegistry,
    SQLiteStateStore,
    TurnEnvelope,
    TurnProcessor,
)
from roost.backends import SANDBOX_LABEL
from roost.sessions import BootError

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


def snapshot_key(session_id: str) -> str:
    return f"workspace/{session_id}.tar.gz"


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
    """M1 内核 + M3b 编排 + M4 持久化接成一套可跑的宿主（测试用最小装配）。"""

    def __init__(self, snapshot_dir: Path) -> None:
        self.backend = DockerSandboxBackend(image=IMAGE)
        self.store = SQLiteStateStore(None)
        self.snapshots = FileSnapshotStore(snapshot_dir)
        self.sink = RecordingSink()
        self.ops = RecordingOps()
        self.backup = BackupCoordinator(self.snapshots, snapshot_key, ops=self.ops)
        self.registry = SessionSandboxRegistry(
            self.backend,
            self.store,
            sink=self.sink,
            ops=self.ops,
            snapshot_store=self.snapshots,
            snapshot_key=snapshot_key,
            template=IMAGE,
            boot_timeout=BOOT_TIMEOUT,
        )
        self.runner = SandboxTurnRunner(self.registry, self.sink, backup=self.backup)
        self.delivery = InProcessTurnDelivery()
        self.processor = TurnProcessor(self.store, self.runner, delivery=self.delivery)
        self.delivery.start(self.processor.process)

    async def count(self, session_id: str, turn_id: str) -> None:
        """跑一个 counter turn，并等这一轮的备份真的写完。"""
        await self.delivery.enqueue(
            TurnEnvelope(
                turn_id=turn_id,
                session_id=session_id,
                payload={"text": "tick", "counter": True},
            )
        )
        await self.delivery.join()
        await self.backup.drain()

    async def close(self) -> None:
        await self.delivery.stop()
        await self.backup.drain()
        await self.store.close()


@pytest.fixture
async def host(sandbox_cleanup, tmp_path: Path):
    harness = Host(tmp_path / "snapshots")
    try:
        yield harness
    finally:
        await harness.close()


async def test_workspace_survives_sandbox_destruction(host) -> None:
    """Demo 2：counter=1 → 备份 → docker rm -f → 新沙箱恢复 → counter=2。"""
    session = f"s-{uuid.uuid4().hex[:8]}"

    await host.count(session, "turn-1")
    assert host.sink.text() == "tick counter=1"
    assert await host.snapshots.get(snapshot_key(session)) is not None

    first = await host.store.get_binding(session)
    assert first is not None
    subprocess.run(
        ["docker", "rm", "-f", first.sandbox_id], capture_output=True, timeout=120
    )
    assert not _container_exists(first.sandbox_id)

    await host.count(session, "turn-2")

    second = await host.store.get_binding(session)
    assert second is not None and second.sandbox_id != first.sandbox_id
    # 续增而不是重来：新沙箱里的 counter 文件来自上一次备份。
    assert host.sink.text() == "tick counter=1tick counter=2"
    assert "workspace_restored" in host.ops.kinds()
    assert host.ops.kinds().count("workspace_backup_finished") == 2


async def test_corrupt_snapshot_fails_boot_and_leaves_no_container(
    host, sandbox_cleanup
) -> None:
    """恢复失败 = boot 失败：抛出，且绝不留下一个未绑定的活容器。"""
    session = f"s-{uuid.uuid4().hex[:8]}"
    await host.snapshots.put(snapshot_key(session), b"not a tar.gz at all")

    before = _labelled_containers()
    # 恢复失败在协议层表现为 `PUT /v1/workspace` 的 500，沿 cold boot 的失败路径原样
    # 抛给调用方（沙箱已被 kill）——不假装 boot 成功，也不吞掉原因。
    with pytest.raises((ControlError, BootError)) as excinfo:
        await host.registry.get_or_create(session, turn_id="turn-boom")
    assert "workspace" in str(excinfo.value)

    assert await host.store.get_binding(session) is None
    survivors = {
        sandbox_id
        for sandbox_id in _labelled_containers() - before
        if _container_exists(sandbox_id)
    }
    assert survivors == set()
    assert "sandbox_boot_failed" in host.ops.kinds()
