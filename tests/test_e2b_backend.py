"""E2BSandboxBackend 端到端测试（真跑 E2B 云沙箱）。

防回归目标：附录 J 钉死的 port 映射在真实 E2B 上成立——create→exec、
upload→读回、detached 服务经沙箱端口 host URL 打通（HTTPS）、pause→connect
隐含恢复、kill 不留沙箱——外加一条编排冒烟（cold boot + 单 turn），证明
driver 装得进去、控制面在 E2B 的端口代理后面通得了。

没有 `ROOST_E2B_API_KEY` 时整文件 skip（契约钉死的凭据环境变量）。每个沙箱都在
夹具里兜底 kill，且测试末尾核对本次创建的沙箱在 E2B 侧确已消失——云沙箱是计费
资源，泄漏一个都不行。key 值绝不进入任何断言、日志或输出。
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from roost import (
    DisplayEvent,
    DriverInstaller,
    InProcessTurnDelivery,
    SandboxTurnRunner,
    SessionSandboxRegistry,
    SQLiteStateStore,
    TurnEnvelope,
    TurnProcessor,
)
from roost.backends import SandboxNotFoundError, SandboxTimeoutError
from roost.backends.e2b import DEFAULT_BIND_HOST, E2BSandboxBackend
from roost.backends.e2b_sdk import ENV_API_KEY

CONTROL_PORT = 8787
SANDBOX_TIMEOUT = 300          # E2B 侧的沙箱存活上限（秒）
BOOT_TIMEOUT = 180.0

pytestmark = pytest.mark.skipif(
    not os.environ.get(ENV_API_KEY), reason=f"{ENV_API_KEY} unset"
)


def _new_backend() -> E2BSandboxBackend:
    return E2BSandboxBackend(control_port=CONTROL_PORT, sandbox_timeout=SANDBOX_TIMEOUT)


@pytest.fixture
async def tracker():
    """记录本次用例创建的沙箱，退出时兜底 kill 并核对 E2B 侧确已消失。"""
    created: list[str] = []
    cleaner = _new_backend()
    yield created
    for sandbox_id in created:
        try:
            await cleaner.kill(await cleaner.connect(sandbox_id))
        except SandboxNotFoundError:
            continue        # 用例自己 kill 过了，正是期望
        except Exception as exc:  # noqa: BLE001 - 清理失败要看得见，但不该盖掉用例结论
            pytest.fail(f"leaked E2B sandbox {sandbox_id}: {exc!r}")
    for sandbox_id in created:
        assert not await _exists(cleaner, sandbox_id), f"sandbox {sandbox_id} survived"


@pytest.fixture
def backend(tracker) -> E2BSandboxBackend:
    return _TrackingBackend(tracker)


class _TrackingBackend(E2BSandboxBackend):
    """把 create 出来的 sandbox_id 登记给清理夹具。"""

    def __init__(self, created: list[str]) -> None:
        super().__init__(control_port=CONTROL_PORT, sandbox_timeout=SANDBOX_TIMEOUT)
        self._created = created

    async def create(self, *, template: str | None = None):
        handle = await super().create(template=template)
        self._created.append(handle.sandbox_id)
        return handle


async def _exists(backend: E2BSandboxBackend, sandbox_id: str) -> bool:
    """存在性以 E2B 自己的 connect 为准（已 kill 的沙箱 connect 必然 not found）。"""
    try:
        await backend.connect(sandbox_id)
    except SandboxNotFoundError:
        return False
    return True


@pytest.fixture
async def sandbox(backend: E2BSandboxBackend):
    handle = await backend.create()
    try:
        yield handle
    finally:
        try:
            await backend.kill(handle)
        except SandboxNotFoundError:
            pass


# -- port 映射 -----------------------------------------------------------


async def test_create_then_exec_echoes(backend, sandbox) -> None:
    assert sandbox.backend == "e2b"
    assert sandbox.sandbox_id

    returncode, stdout, stderr = await backend.exec(
        sandbox, ["sh", "-c", "echo $GREETING"], env={"GREETING": "hello-roost"}
    )

    assert (returncode, stdout.strip(), stderr) == (0, "hello-roost", "")


async def test_exec_reports_nonzero_and_stderr(backend, sandbox) -> None:
    returncode, stdout, stderr = await backend.exec(
        sandbox, ["sh", "-c", "echo oops >&2; exit 3"]
    )

    assert returncode == 3
    assert stdout == ""
    assert "oops" in stderr


async def test_exec_timeout_raises(backend, sandbox) -> None:
    with pytest.raises(SandboxTimeoutError):
        await backend.exec(sandbox, ["sleep", "30"], timeout_seconds=2.0)


async def test_upload_then_exec_reads_back(backend, sandbox) -> None:
    await backend.upload(
        sandbox,
        {
            "/home/user/roost/hello.txt": b"uploaded-body\n",
            "/home/user/roost/nested/deep.bin": bytes(range(256)),
        },
    )

    returncode, stdout, _ = await backend.exec(
        sandbox, ["cat", "/home/user/roost/hello.txt"]
    )
    assert (returncode, stdout) == (0, "uploaded-body\n")

    returncode, stdout, _ = await backend.exec(
        sandbox, ["wc", "-c", "/home/user/roost/nested/deep.bin"]
    )
    assert returncode == 0
    assert stdout.split()[0] == "256"


async def test_request_reaches_detached_http_server(backend, sandbox) -> None:
    """控制端口经 E2B 的端口代理（HTTPS）打通，且 driver 只监听沙箱内 loopback。"""
    await backend.upload(sandbox, {"/home/user/http/probe.txt": b"served-by-sandbox"})
    returncode, _, stderr = await backend.exec(
        sandbox,
        [
            "sh",
            "-c",
            f"nohup python3 -m http.server {CONTROL_PORT} --bind {DEFAULT_BIND_HOST} "
            "--directory /home/user/http >/dev/null 2>&1 &",
        ],
    )
    assert returncode == 0, stderr

    status, body = await _request_until_up(backend, sandbox, "/probe.txt")
    assert (status, body) == (200, b"served-by-sandbox")

    status, _ = await backend.request(sandbox, "GET", "/missing.txt", timeout_seconds=20)
    assert status == 404


async def test_pause_then_connect_resumes(backend, sandbox) -> None:
    await backend.pause(sandbox)

    reconnected = await backend.connect(sandbox.sandbox_id)

    assert reconnected == sandbox
    returncode, stdout, _ = await backend.exec(reconnected, ["echo", "awake"])
    assert (returncode, stdout.strip()) == (0, "awake")


async def test_connect_unknown_sandbox_raises(backend) -> None:
    with pytest.raises(SandboxNotFoundError):
        await backend.connect(f"roostmissing{uuid.uuid4().hex[:12]}")


async def test_kill_removes_sandbox(backend) -> None:
    handle = await backend.create()

    await backend.kill(handle)

    assert not await _exists(backend, handle.sandbox_id)


# -- 编排冒烟 -------------------------------------------------------------


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[DisplayEvent] = []

    async def emit(self, events: list[DisplayEvent]) -> None:
        self.events.extend(events)

    def of_kind(self, kind: str) -> list[DisplayEvent]:
        return [event for event in self.events if event.kind == kind]

    def text(self) -> str:
        return "".join(event.body["text"] for event in self.of_kind("text"))


async def test_cold_boot_then_turn_on_e2b(backend, tracker) -> None:
    """cold boot 一个真 E2B 沙箱 + 跑完一个 turn，事件流以 Terminal 收尾。"""
    session = f"s-{uuid.uuid4().hex[:8]}"
    store = SQLiteStateStore(None)
    sink = RecordingSink()
    registry = SessionSandboxRegistry(
        backend,
        store,
        sink=sink,
        installer=DriverInstaller(bind_host=DEFAULT_BIND_HOST),
        boot_timeout=BOOT_TIMEOUT,
        health_timeout=10.0,
        request_timeout=30.0,
    )
    runner = SandboxTurnRunner(registry, sink)
    delivery = InProcessTurnDelivery()
    processor = TurnProcessor(store, runner, delivery=delivery)
    delivery.start(processor.process)
    try:
        await delivery.enqueue(
            TurnEnvelope(turn_id="turn-1", session_id=session, payload={"text": "hello e2b"})
        )
        await delivery.join()

        assert sink.text() == "hello e2b"
        terminals = sink.of_kind("terminal")
        assert len(terminals) == 1
        assert terminals[0].body["status"] == "ok"
        assert terminals[0] is sink.events[-1]

        kinds = [event.body["kind"] for event in sink.of_kind("lifecycle_notice")]
        assert kinds == ["boot_started", "boot_finished"]

        seqs = [event.seq for event in sink.events]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

        binding = await store.get_binding(session)
        assert binding is not None and binding.backend == "e2b"
    finally:
        await delivery.stop()
        await store.close()


async def _request_until_up(backend, handle, path: str, attempts: int = 40):
    """detached 服务需要一点起动时间；轮询到第一次成功响应。"""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            status, body = await backend.request(handle, "GET", path, timeout_seconds=20)
        except OSError as exc:  # URLError 是 OSError 子类
            last = exc
            await asyncio.sleep(0.5)
            continue
        if status == 502:       # 端口代理在服务起来之前会给 502
            await asyncio.sleep(0.5)
            continue
        return status, body
    raise AssertionError(f"sandbox http server never came up: {last!r}")
