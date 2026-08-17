"""M6 端到端：runtime 过期的沙箱被零停机替换（真 docker + 真 driver）。

防的回归（CONTRACTS.md 附录 I / DESIGN.md I3 后半）：

- **成功替换**：turn1 的 counter=1 落在沙箱 A；换一份 fingerprint 不同的 runtime
  之后，turn2 自动在**新沙箱 B** 上执行、counter **续增到 2**、A 已销毁、绑定与
  stamp 都指向新版本。counter 是"状态确实跟着走了"的最小观测面——而且这一轮刻意
  **抹掉 SnapshotStore 里的快照**：状态只能来自旧沙箱的活内存快照，走存储层的
  实现会在这里变成空工作区、counter 退回 1。
- **失败永不伤 turn**：新版本根本起不来时，turn2 仍然在 A 上正常答复（counter 照增）、
  A 仍然绑定且活着、半成品容器不残留；随后 backoff 生效，turn3 不再重试。
- **CAS 失败不覆盖别人的绑定**：换绑期间别的执行者抢先换绑时，本次 boot 出来的
  容器必须被销毁，绑定保持别人写下的那一个，本 turn 照常由旧沙箱答复。

全程走 delivery → TurnProcessor → SandboxTurnRunner 这条真实链路（与 M4 的持久化
e2e 同构）：替换发生在 `get_or_create` 里，只有让 runner 真的去要沙箱才测得到。
本机无 docker 时整文件 skip；夹具兜底清理本次新建的容器（roost.sandbox=1 label）。
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from roost import (
    BackupCoordinator,
    DisplayEvent,
    DockerSandboxBackend,
    DriverInstaller,
    FileSnapshotStore,
    InProcessTurnDelivery,
    SandboxHandle,
    SandboxTurnRunner,
    SessionSandboxRegistry,
    SQLiteStateStore,
    TurnEnvelope,
    TurnProcessor,
    runtime_fingerprint,
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


def snapshot_key(session_id: str) -> str:
    return f"workspace/{session_id}.tar.gz"


class MarkedInstaller(DriverInstaller):
    """"新版本"的 runtime：往文件表里加一个真实存在的标记模块。

    fingerprint 因此改变，而且改变的理由是真实的——这份字节确实会被上传进沙箱，
    和"改了一行 driver 源码"在系统看来是同一类事件。
    """

    def __init__(self, marker: bytes) -> None:
        super().__init__()
        self._marker = marker

    @property
    def files(self) -> dict[str, bytes]:
        files = super().files
        files[f"{self.package_dir}/_build_marker.py"] = self._marker
        return files


class BrokenInstaller(MarkedInstaller):
    """新版本起不来：文件照传，启动命令必败（最贴近"新 runtime 有毒"的现实）。"""

    def start_command(self) -> list[str]:
        return ["sh", "-c", "exit 3"]


class RacingStore:
    """在 swap_binding 之前插入一次真实的竞争换绑，制造 CAS 失败。"""

    def __init__(self, inner: SQLiteStateStore) -> None:
        self._inner = inner
        self.winner: SandboxHandle | None = None

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def swap_binding(self, session_id, old, new, stamp) -> bool:
        if self.winner is not None:
            competitor, self.winner = self.winner, None
            assert await self._inner.swap_binding(session_id, old, competitor, stamp)
        return await self._inner.swap_binding(session_id, old, new, stamp)


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[DisplayEvent] = []

    async def emit(self, events: list[DisplayEvent]) -> None:
        self.events.extend(events)

    def of_kind(self, kind: str) -> list[DisplayEvent]:
        return [event for event in self.events if event.kind == kind]

    def lifecycle_kinds(self) -> list[str]:
        return [event.body["kind"] for event in self.of_kind("lifecycle_notice")]

    def text(self) -> str:
        return "".join(event.body["text"] for event in self.of_kind("text"))


class RecordingOps:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type: str, /, **details: object) -> None:
        self.events.append((event_type, dict(details)))

    def kinds(self) -> list[str]:
        return [event for event, _ in self.events]

    def forced(self) -> list[str]:
        return [k for k in self.kinds() if k.startswith("forced_update_")]


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
    """一个"宿主进程"：同一套 store/快照可以被换了 runtime 的第二个 Host 接手。"""

    def __init__(
        self,
        snapshot_dir: Path,
        *,
        installer: DriverInstaller | None = None,
        store=None,
        snapshots: FileSnapshotStore | None = None,
        registry_store=None,
    ) -> None:
        self.backend = DockerSandboxBackend(image=IMAGE)
        self.store = store if store is not None else SQLiteStateStore(None)
        self.snapshots = (
            snapshots if snapshots is not None else FileSnapshotStore(snapshot_dir)
        )
        self.sink = RecordingSink()
        self.ops = RecordingOps()
        self.backup = BackupCoordinator(self.snapshots, snapshot_key, ops=self.ops)
        self.registry = SessionSandboxRegistry(
            self.backend,
            registry_store if registry_store is not None else self.store,
            installer=installer,
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

    def successor(self, installer: DriverInstaller, *, registry_store=None) -> "Host":
        """换了 runtime 的新一代宿主，接手同一套 source of truth。"""
        return Host(
            Path("."),
            installer=installer,
            store=self.store,
            snapshots=self.snapshots,
            registry_store=registry_store,
        )

    async def count(self, session_id: str, turn_id: str) -> None:
        await self.delivery.enqueue(
            TurnEnvelope(
                turn_id=turn_id,
                session_id=session_id,
                payload={"text": "tick", "counter": True},
            )
        )
        await self.delivery.join()
        await self.backup.drain()

    async def close(self, *, close_store: bool = True) -> None:
        await self.delivery.stop()
        await self.backup.drain()
        if close_store:
            await self.store.close()


@pytest.fixture
async def hosts(sandbox_cleanup, tmp_path: Path):
    """一代宿主 + 它的后继们；后继共享 store，故只由第一代负责关闭。"""
    first = Host(tmp_path / "snapshots")
    spawned: list[Host] = [first]

    def spawn(installer: DriverInstaller, *, registry_store=None) -> Host:
        host = first.successor(installer, registry_store=registry_store)
        spawned.append(host)
        return host

    try:
        yield first, spawn
    finally:
        for host in reversed(spawned):
            await host.close(close_store=host is first)


async def test_stale_runtime_is_replaced_between_turns(hosts, tmp_path: Path) -> None:
    """成功替换：counter 经活快照续增，旧沙箱换绑后才销毁。"""
    old_host, spawn = hosts
    session = f"s-{uuid.uuid4().hex[:8]}"

    await old_host.count(session, "turn-1")
    assert old_host.sink.text() == "tick counter=1"
    first = await old_host.store.get_binding(session)
    assert first is not None
    stamp = await old_host.store.get_stamp(session)
    assert stamp is not None
    assert stamp.runtime_files_hash == runtime_fingerprint(DriverInstaller())

    # 抹掉存储层的快照：状态若不是从旧沙箱活取的，这里就只能变回空工作区。
    old_host.snapshots.path_for(snapshot_key(session)).unlink()
    assert await old_host.snapshots.get(snapshot_key(session)) is None

    installer = MarkedInstaller(b"BUILD = 2\n")
    new_host = spawn(installer)
    await new_host.count(session, "turn-2")

    second = await new_host.store.get_binding(session)
    assert second is not None and second.sandbox_id != first.sandbox_id
    assert new_host.sink.text() == "tick counter=2"          # 状态跟着走了
    assert new_host.sink.lifecycle_kinds() == ["update_started", "update_finished"]
    assert [e.seq for e in new_host.sink.of_kind("lifecycle_notice")] == [3, 4]
    assert new_host.ops.forced() == ["forced_update_completed"]
    assert not _container_exists(first.sandbox_id)           # 旧沙箱已销毁
    assert _container_exists(second.sandbox_id)

    stamp = await new_host.store.get_stamp(session)
    assert stamp is not None
    assert stamp.runtime_files_hash == runtime_fingerprint(installer)


async def test_failed_update_keeps_serving_on_old_sandbox_then_backs_off(
    hosts,
) -> None:
    """新 runtime 起不来：turn 照常在旧沙箱上完成，随后 backoff 停止重试。"""
    old_host, spawn = hosts
    session = f"s-{uuid.uuid4().hex[:8]}"

    await old_host.count(session, "turn-1")
    first = await old_host.store.get_binding(session)
    assert first is not None

    new_host = spawn(BrokenInstaller(b"BUILD = 'broken'\n"))
    before = _labelled_containers()
    await new_host.count(session, "turn-2")

    # 本 turn 毫发无伤：仍在 A 上、counter 照增、A 仍然绑定且活着。
    assert new_host.sink.text() == "tick counter=2"
    assert await new_host.store.get_binding(session) == first
    assert _container_exists(first.sandbox_id)
    assert new_host.sink.lifecycle_kinds() == []             # 失败不发 update 通告
    assert new_host.ops.forced() == ["forced_update_failed"]
    # 半成品容器不残留。
    survivors = {
        sandbox_id
        for sandbox_id in _labelled_containers() - before
        if _container_exists(sandbox_id)
    }
    assert survivors == set()

    await new_host.count(session, "turn-3")

    assert new_host.sink.text() == "tick counter=2tick counter=3"
    assert new_host.ops.forced() == ["forced_update_failed"]  # backoff：没有第二次
    assert await new_host.store.get_binding(session) == first


async def test_cas_conflict_discards_new_sandbox(hosts) -> None:
    """换绑期间别人抢先：新容器销毁、别人的绑定不被覆盖、本 turn 仍由旧沙箱答复。"""
    old_host, spawn = hosts
    session = f"s-{uuid.uuid4().hex[:8]}"

    await old_host.count(session, "turn-1")
    first = await old_host.store.get_binding(session)
    assert first is not None

    racing = RacingStore(old_host.store)
    new_host = spawn(MarkedInstaller(b"BUILD = 3\n"), registry_store=racing)
    racing.winner = SandboxHandle(sandbox_id="sbx-someone-else", backend="docker")
    before = _labelled_containers()

    await new_host.count(session, "turn-2")

    assert new_host.sink.text() == "tick counter=2"           # 旧沙箱照常答复
    binding = await new_host.store.get_binding(session)
    assert binding is not None and binding.sandbox_id == "sbx-someone-else"
    assert new_host.ops.forced() == ["forced_update_failed"]
    assert _container_exists(first.sandbox_id)                # 旧沙箱没被顺手杀掉
    survivors = {
        sandbox_id
        for sandbox_id in _labelled_containers() - before
        if _container_exists(sandbox_id)
    }
    assert survivors == set()                                 # 作废的新容器已销毁
