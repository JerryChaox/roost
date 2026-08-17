"""SessionSandboxRegistry 的编排语义（假 backend，不需要 docker）。

防的回归（附录 F 的 cold boot 段）：

- boot 就绪只认 `/v1/health`；不就绪必须超时失败，**且 kill 半成品沙箱**——
  孤儿容器不在任何 source of truth 里，泄漏了没人回收；
- cold boot 成功后绑定经 `swap_binding` 落库（old=None 分支自此可达）；
- 有绑定且沙箱健康 → 复用，不得再 create；
- 有绑定但沙箱死了 → 自动 cold boot 新沙箱并换绑；
- boot 期的 lifecycle 通告经 EventSink 送出，含 elapsed_ms。

真容器路径由 test_orchestration_e2e.py 覆盖；这里刻意用假 backend，让上述判定
逻辑的回归在毫秒级暴露，而不是被 docker 的噪声掩盖。
"""

from __future__ import annotations

import json

import pytest

from roost import (
    BootTimeoutError,
    SandboxHandle,
    SessionSandboxRegistry,
    SQLiteStateStore,
)
from roost.backends.errors import SandboxNotFoundError

SESSION = "session-1"


class FakeBackend:
    """可控活性的假沙箱：request 只回答 `/v1/health`，其余路径 404。"""

    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.live: set[str] = set()
        self.created: list[str] = []
        self.killed: list[str] = []
        self.uploads: list[tuple[str, dict[str, bytes]]] = []
        self.execs: list[tuple[str, list[str]]] = []
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
        self.uploads.append((handle.sandbox_id, files))

    async def exec(self, handle, argv, *, env=None, timeout_seconds=None):
        del env, timeout_seconds
        self.execs.append((handle.sandbox_id, list(argv)))
        return 0, "", ""

    async def request(
        self, handle, method, path, *, body=None, headers=None, timeout_seconds=None
    ):
        del method, body, headers, timeout_seconds
        if handle.sandbox_id not in self.live:
            raise ConnectionRefusedError(handle.sandbox_id)
        if not self.ready or not path.startswith("/v1/health"):
            return 404, b'{"error": "not_found"}'
        return 200, json.dumps(
            {
                "ok": True,
                "protocol_version": "1",
                "uptime_ms": 5,
                "harness_ready": True,
            }
        ).encode()


class RecordingSink:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, events: list) -> None:
        self.events.extend(events)


@pytest.fixture
async def store():
    store = SQLiteStateStore(None)
    try:
        yield store
    finally:
        await store.close()


def make_registry(backend: FakeBackend, store, sink=None, **kwargs):
    options = {"boot_timeout": 0.3, "poll_interval": 0.01, "health_timeout": 0.5}
    options.update(kwargs)
    return SessionSandboxRegistry(backend, store, sink=sink, **options)


async def test_cold_boot_binds_uploads_and_starts_driver(store) -> None:
    backend = FakeBackend()
    sink = RecordingSink()
    registry = make_registry(backend, store, sink)

    handle, client = await registry.get_or_create(SESSION, turn_id="t1")

    assert backend.created == [handle.sandbox_id]
    assert await store.get_binding(SESSION) == handle
    assert client is not None

    uploaded_paths = set(backend.uploads[0][1])
    assert "/opt/roost/src/roost/driver/__main__.py" in uploaded_paths
    assert "/opt/roost/src/roost/control/envelope.py" in uploaded_paths
    assert backend.execs[0][1][0] == "sh"
    assert "python -m roost.driver" in backend.execs[0][1][2]


async def test_boot_emits_lifecycle_notices(store) -> None:
    sink = RecordingSink()
    registry = make_registry(FakeBackend(), store, sink)

    await registry.get_or_create(SESSION, turn_id="t1")

    kinds = [e.body["kind"] for e in sink.events]
    assert kinds == ["boot_started", "boot_finished"]
    assert all(e.kind == "lifecycle_notice" for e in sink.events)
    assert all(e.turn_id == "t1" and e.session_id == SESSION for e in sink.events)
    assert sink.events[-1].body["elapsed_ms"] >= 0
    assert sink.events[0].seq < sink.events[1].seq


async def test_healthy_binding_is_reused(store) -> None:
    backend = FakeBackend()
    registry = make_registry(backend, store)

    first, _ = await registry.get_or_create(SESSION)
    second, _ = await registry.get_or_create(SESSION)

    assert first == second
    assert backend.created == [first.sandbox_id]


async def test_dead_sandbox_triggers_cold_boot_and_swap(store) -> None:
    backend = FakeBackend()
    registry = make_registry(backend, store)
    first, _ = await registry.get_or_create(SESSION)

    backend.live.discard(first.sandbox_id)          # 容器没了（等价 docker rm -f）
    second, _ = await registry.get_or_create(SESSION)

    assert second.sandbox_id != first.sandbox_id
    assert await store.get_binding(SESSION) == second


async def test_unhealthy_driver_counts_as_dead_sandbox(store) -> None:
    """容器还在、driver 死了：活性判定必须以 health 为准，不能只看 connect。"""
    backend = FakeBackend()
    registry = make_registry(backend, store)
    first, _ = await registry.get_or_create(SESSION)

    backend.ready = False                            # 容器仍 live，health 不通
    with pytest.raises(BootTimeoutError):
        await registry.get_or_create(SESSION)

    assert len(backend.created) == 2                 # 确实尝试了 cold boot
    assert backend.killed == [backend.created[1]]    # 半成品被清理
    assert await store.get_binding(SESSION) == first  # 旧绑定未被覆盖


async def test_boot_timeout_kills_sandbox_and_leaves_no_binding(store) -> None:
    backend = FakeBackend(ready=False)
    registry = make_registry(backend, store)

    with pytest.raises(BootTimeoutError):
        await registry.get_or_create(SESSION)

    assert backend.killed == backend.created
    assert not backend.live
    assert await store.get_binding(SESSION) is None
