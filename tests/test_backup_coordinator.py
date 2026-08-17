"""BackupCoordinator：turn 边界的异步备份。

防的回归（CONTRACTS.md 附录 G 的验收面 + DESIGN.md I2）：

- 成功路径把 `GET /v1/workspace` 的字节写进 SnapshotStore 的 `key_fn(session_id)`；
- **失败绝不外溢**：schedule 不抛、drain 不抛，失败经 OpsRecorder 留痕——这是 I2
  "写失败不影响 turn 结果"的可执行形态；
- **同 session 并发去重**：前一次还在跑时本次跳过，而不是排队。

这里用假的 ControlClient/SnapshotStore：被测的是调度语义（谁跑、跑几次、失败怎么
处理），真实的 HTTP 与落盘分别由 workspace 端点测试与 SnapshotStore 契约测试覆盖。
"""

from __future__ import annotations

import asyncio

import pytest

from roost import BackupCoordinator


class FakeStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.items: dict[str, bytes] = {}
        self._fail = fail

    async def put(self, key: str, data: bytes) -> None:
        if self._fail:
            raise OSError("disk on fire")
        self.items[key] = data

    async def get(self, key: str) -> bytes | None:
        return self.items.get(key)


class FakeClient:
    """只提供 get_workspace 的假 ControlClient。"""

    def __init__(self, data: bytes = b"gz", *, error: Exception | None = None) -> None:
        self._data = data
        self._error = error
        self.calls = 0
        self.gate: asyncio.Event | None = None

    async def get_workspace(self) -> bytes:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self._error is not None:
            raise self._error
        return self._data


class RecordingOps:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type: str, /, **details: object) -> None:
        self.events.append((event_type, dict(details)))

    def kinds(self) -> list[str]:
        return [event for event, _ in self.events]


def key_fn(session_id: str) -> str:
    return f"workspace/{session_id}.tar.gz"


async def test_schedule_writes_workspace_bytes_to_store() -> None:
    store, ops = FakeStore(), RecordingOps()
    coordinator = BackupCoordinator(store, key_fn, ops=ops)

    coordinator.schedule("s-1", FakeClient(b"payload"))
    await coordinator.drain()

    assert store.items == {"workspace/s-1.tar.gz": b"payload"}
    assert "workspace_backup_finished" in ops.kinds()
    assert coordinator.pending == 0


@pytest.mark.parametrize(
    ("client", "store"),
    [
        (FakeClient(error=RuntimeError("driver gone")), FakeStore()),
        (FakeClient(b"payload"), FakeStore(fail=True)),
    ],
)
async def test_backup_failure_never_escapes_and_is_recorded(client, store) -> None:
    ops = RecordingOps()
    coordinator = BackupCoordinator(store, key_fn, ops=ops)

    coordinator.schedule("s-1", client)      # 不抛
    await coordinator.drain()                # 不抛

    assert store.items == {}
    assert "workspace_backup_failed" in ops.kinds()
    assert "workspace_backup_finished" not in ops.kinds()


async def test_concurrent_schedule_for_same_session_is_deduplicated() -> None:
    store, ops = FakeStore(), RecordingOps()
    coordinator = BackupCoordinator(store, key_fn, ops=ops)
    client = FakeClient(b"payload")
    client.gate = asyncio.Event()

    coordinator.schedule("s-1", client)
    await asyncio.sleep(0)                   # 让第一次备份跑到 gate 上挂住
    coordinator.schedule("s-1", client)      # 已在跑 → 跳过

    client.gate.set()
    await coordinator.drain()

    assert client.calls == 1
    assert ops.kinds().count("workspace_backup_skipped") == 1
    assert ops.kinds().count("workspace_backup_finished") == 1


async def test_distinct_sessions_back_up_independently() -> None:
    store = FakeStore()
    coordinator = BackupCoordinator(store, key_fn)

    coordinator.schedule("s-1", FakeClient(b"one"))
    coordinator.schedule("s-2", FakeClient(b"two"))
    await coordinator.drain()

    assert store.items == {
        "workspace/s-1.tar.gz": b"one",
        "workspace/s-2.tar.gz": b"two",
    }


async def test_sequential_schedules_run_again_after_previous_finished() -> None:
    """去重只在"还在跑"期间生效——下一个 turn 边界必须能再备一次。"""
    store = FakeStore()
    coordinator = BackupCoordinator(store, key_fn)
    client = FakeClient(b"one")

    coordinator.schedule("s-1", client)
    await coordinator.drain()
    coordinator.schedule("s-1", client)
    await coordinator.drain()

    assert client.calls == 2
