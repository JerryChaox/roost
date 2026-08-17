"""forced update 的替换语义与失败回退（假 backend，不需要 docker）。

防的回归（CONTRACTS.md 附录 I / DESIGN.md I3 后半"失败永不伤 turn"）：

- **CAS 失败绝不覆盖别人的绑定**：换绑期间别的执行者已经把 session 绑到了另一个
  沙箱时，本次 boot 出来的新沙箱必须被 kill，绑定保持别人写下的那一个。这条是
  整个 M6 里最危险的一步——覆盖过去意味着两个执行者各自以为自己独占该 session。
- **每一条失败路径都退回旧沙箱**：取活快照失败 / 新沙箱 boot 失败 / CAS 失败，
  三者都必须让调用方拿到**原来那个健康的旧沙箱**，而不是异常、也不是半成品。
- **失败后进入 backoff**：否则"新版本起不来"会从一次失败变成每个 turn 都白跑一次
  cold boot——一个更新故障就此降级成持续的性能故障。
- **失败路径不发 update 通告**：旧沙箱继续答题，显示流里不该出现悬空的"更新中"。

真容器路径由 test_forced_update_e2e.py 覆盖；这里用假 backend 让上述判定的回归在
毫秒级暴露。CAS 失败刻意用**真 SQLiteStateStore + 一个真的竞争写入**构造，而不是
让 fake 直接返回 False——要测的正是真实 CAS 谓词的行为。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from roost import (
    RuntimeStamp,
    SandboxHandle,
    SessionSandboxRegistry,
    SQLiteStateStore,
    runtime_fingerprint,
)
from roost.backends.errors import SandboxNotFoundError
from roost.install import DriverInstaller

SESSION = "session-1"
STALE = "sha256:stale"


class FakeBackend:
    """假沙箱：health 恒 ok，workspace 端点可读可写，内容按 sandbox_id 区分。"""

    def __init__(self) -> None:
        self.live: set[str] = set()
        self.created: list[str] = []
        self.killed: list[str] = []
        self.restored: dict[str, bytes] = {}
        self.boot_fails = False
        self.workspace_fails = False
        self._next = 0

    async def create(self, *, template: str | None = None) -> SandboxHandle:
        del template
        self._next += 1
        sandbox_id = f"sbx-{self._next}"
        self.live.add(sandbox_id)
        self.created.append(sandbox_id)
        return SandboxHandle(sandbox_id=sandbox_id, backend="fake")

    async def connect(self, sandbox_id: str) -> SandboxHandle:
        if sandbox_id not in self.live:
            raise SandboxNotFoundError(sandbox_id)
        return SandboxHandle(sandbox_id=sandbox_id, backend="fake")

    async def pause(self, handle: SandboxHandle) -> None:
        raise NotImplementedError

    async def kill(self, handle: SandboxHandle) -> None:
        self.killed.append(handle.sandbox_id)
        self.live.discard(handle.sandbox_id)

    async def upload(self, handle: SandboxHandle, files: dict[str, bytes]) -> None:
        del handle, files

    async def exec(self, handle, argv, *, env=None, timeout_seconds=None):
        del handle, argv, env, timeout_seconds
        if self.boot_fails:
            return 1, "", "driver refused to start"
        return 0, "", ""

    async def request(
        self, handle, method, path, *, body=None, headers=None, timeout_seconds=None
    ):
        del headers, timeout_seconds
        if handle.sandbox_id not in self.live:
            raise ConnectionRefusedError(handle.sandbox_id)
        if path.startswith("/v1/health"):
            return 200, json.dumps(
                {
                    "ok": True,
                    "protocol_version": "1",
                    "uptime_ms": 5,
                    "harness_ready": True,
                }
            ).encode()
        if path.startswith("/v1/workspace"):
            if self.workspace_fails:
                return 500, b'{"error": "workspace_pack_failed"}'
            if method == "GET":
                return 200, f"workspace-of-{handle.sandbox_id}".encode()
            self.restored[handle.sandbox_id] = body or b""
            return 200, b"{}"
        return 404, b'{"error": "not_found"}'


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
        self.events: list = []

    async def emit(self, events: list) -> None:
        self.events.extend(events)

    def kinds(self) -> list[str]:
        return [event.body["kind"] for event in self.events]


class RecordingOps:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type: str, /, **details: object) -> None:
        self.events.append((event_type, dict(details)))

    def kinds(self) -> list[str]:
        return [event for event, _ in self.events]


@pytest.fixture
async def store():
    store = SQLiteStateStore(None)
    try:
        yield store
    finally:
        await store.close()


def make_stamp(runtime_files_hash: str | None) -> RuntimeStamp:
    return RuntimeStamp(
        bound_at=datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc),
        template_id=None,
        runtime_files_hash=runtime_files_hash,
    )


def make_registry(backend, store, sink=None, ops=None, **kwargs):
    options = {"boot_timeout": 1.0, "poll_interval": 0.01, "health_timeout": 0.5}
    options.update(kwargs)
    return SessionSandboxRegistry(backend, store, sink=sink, ops=ops, **options)


async def bind_stale(backend: FakeBackend, store, *, stamp_hash: str | None = STALE):
    """给 session 装一个健康的、runtime 过期的既有沙箱。"""
    old = await backend.create()
    await store.bind(SESSION, old, make_stamp(stamp_hash))
    return old


# -- 触发条件 ------------------------------------------------------------


async def test_current_fingerprint_does_not_trigger_replacement(store) -> None:
    backend = FakeBackend()
    current = runtime_fingerprint(DriverInstaller())
    old = await bind_stale(backend, store, stamp_hash=current)
    registry = make_registry(backend, store)

    handle, _ = await registry.get_or_create(SESSION, turn_id="t1")

    assert handle == old
    assert backend.created == [old.sandbox_id]     # 没有多 boot 一个沙箱


async def test_legacy_none_stamp_does_not_trigger_replacement(store) -> None:
    """hash 为 None = 豁免比对（快照烘焙 / legacy 绑定），M6 不做迁移。"""
    backend = FakeBackend()
    old = await bind_stale(backend, store, stamp_hash=None)
    registry = make_registry(backend, store)

    handle, _ = await registry.get_or_create(SESSION, turn_id="t1")

    assert handle == old
    assert backend.created == [old.sandbox_id]


# -- 成功路径 ------------------------------------------------------------


async def test_stale_runtime_is_replaced_with_live_workspace(store) -> None:
    backend, sink, ops = FakeBackend(), RecordingSink(), RecordingOps()
    old = await bind_stale(backend, store)
    registry = make_registry(backend, store, sink, ops)

    handle, _ = await registry.get_or_create(SESSION, turn_id="t1")

    assert handle.sandbox_id != old.sandbox_id
    assert await store.get_binding(SESSION) == handle
    # 活快照取自旧沙箱、灌进新沙箱——不经 SnapshotStore（这里根本没配）。
    assert backend.restored[handle.sandbox_id] == f"workspace-of-{old.sandbox_id}".encode()
    # 换绑成功之后才杀旧沙箱。
    assert backend.killed == [old.sandbox_id]
    stamp = await store.get_stamp(SESSION)
    assert stamp is not None
    assert stamp.runtime_files_hash == runtime_fingerprint(DriverInstaller())
    assert sink.kinds() == ["update_started", "update_finished"]
    assert [e.seq for e in sink.events] == [3, 4]
    assert "forced_update_completed" in ops.kinds()


# -- 失败路径：全部退回旧沙箱 ----------------------------------------------


async def test_cas_failure_kills_new_sandbox_and_keeps_other_binding(store) -> None:
    """别人已经换过绑：新沙箱作废，绝不覆盖别人的绑定，本 turn 用旧沙箱。"""
    backend, sink, ops = FakeBackend(), RecordingSink(), RecordingOps()
    old = await bind_stale(backend, store)
    racing = RacingStore(store)
    racing.winner = SandboxHandle(sandbox_id="sbx-other", backend="fake")
    registry = make_registry(backend, racing, sink, ops)

    handle, client = await registry.get_or_create(SESSION, turn_id="t1")

    assert handle == old and client is not None          # 本 turn 照常跑在旧沙箱上
    assert (await store.get_binding(SESSION)).sandbox_id == "sbx-other"
    loser = backend.created[-1]
    assert loser not in backend.live and loser in backend.killed
    assert old.sandbox_id not in backend.killed          # 旧沙箱没被顺手杀掉
    assert sink.events == []                             # 失败路径不发 update 通告
    assert "forced_update_failed" in ops.kinds()
    assert "forced_update_completed" not in ops.kinds()


async def test_boot_failure_falls_back_to_old_sandbox_and_backs_off(store) -> None:
    backend, sink, ops = FakeBackend(), RecordingSink(), RecordingOps()
    old = await bind_stale(backend, store)
    backend.boot_fails = True
    registry = make_registry(backend, store, sink, ops)

    handle, client = await registry.get_or_create(SESSION, turn_id="t1")

    assert handle == old and client is not None
    assert await store.get_binding(SESSION) == old
    assert old.sandbox_id in backend.live
    assert backend.created[-1] in backend.killed          # 半成品已清理
    assert sink.events == []
    assert ops.kinds().count("forced_update_failed") == 1

    # backoff 生效：下一个 turn 不再重试（沙箱数不变、也没有第二条 forced_update_*）。
    created_before = len(backend.created)
    await registry.get_or_create(SESSION, turn_id="t2")
    assert len(backend.created) == created_before
    assert [k for k in ops.kinds() if k.startswith("forced_update_")] == [
        "forced_update_failed"
    ]


async def test_snapshot_failure_aborts_without_creating_a_sandbox(store) -> None:
    """取活快照就失败时，连新沙箱都不该建——没有活快照就没有可恢复的状态。"""
    backend, sink, ops = FakeBackend(), RecordingSink(), RecordingOps()
    old = await bind_stale(backend, store)
    backend.workspace_fails = True
    registry = make_registry(backend, store, sink, ops)

    handle, _ = await registry.get_or_create(SESSION, turn_id="t1")

    assert handle == old
    assert backend.created == [old.sandbox_id]
    assert backend.killed == []
    assert sink.events == []
    assert "forced_update_aborted" in ops.kinds()


async def test_backoff_zero_retries_every_turn(store) -> None:
    """backoff 可关：构造参数为 0 时每个 turn 都重试（e2e 与运维需要这个开关）。"""
    backend, ops = FakeBackend(), RecordingOps()
    await bind_stale(backend, store)
    backend.workspace_fails = True
    registry = make_registry(backend, store, ops=ops, update_backoff=0)

    await registry.get_or_create(SESSION, turn_id="t1")
    await registry.get_or_create(SESSION, turn_id="t2")

    assert ops.kinds().count("forced_update_aborted") == 2


async def test_negative_backoff_is_rejected(store) -> None:
    with pytest.raises(ValueError):
        make_registry(FakeBackend(), store, update_backoff=-1)


async def test_put_workspace_failure_on_new_sandbox_falls_back(store) -> None:
    """恢复失败也是 boot 失败：新沙箱被 kill，旧沙箱继续服务（不抛给调用方）。"""
    backend, ops = FakeBackend(), RecordingOps()
    old = await bind_stale(backend, store)

    original_request = backend.request

    async def failing_put(handle, method, path, **kwargs):
        if method == "PUT" and path.startswith("/v1/workspace"):
            return 500, b'{"error": "workspace_unpack_failed"}'
        return await original_request(handle, method, path, **kwargs)

    backend.request = failing_put   # type: ignore[method-assign]
    registry = make_registry(backend, store, ops=ops)

    handle, _ = await registry.get_or_create(SESSION, turn_id="t1")

    assert handle == old
    assert await store.get_binding(SESSION) == old
    assert backend.created[-1] in backend.killed
    assert "forced_update_failed" in ops.kinds()
    # 失败原因确实是恢复失败，而不是被别的步骤吞掉。
    reason = [d for k, d in ops.events if k == "forced_update_failed"][0]["error"]
    assert "workspace" in reason
