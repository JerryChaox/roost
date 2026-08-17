"""DockerSandboxBackend 端到端测试（真跑本机 docker）。

防回归目标：附录 D 钉死的五条外部可观察行为——create→exec、upload→读回、
detached 服务经 request 打通、pause→connect 隐含恢复、kill 不留容器。
本机无 docker 时整文件 skip。夹具兜底 `docker rm -f`，容器均带 roost.sandbox=1
label 便于人工清理。
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid

import pytest

from roost.backends import (
    SANDBOX_LABEL,
    SANDBOX_LABEL_KEY,
    SandboxNotFoundError,
    SandboxTimeoutError,
    DockerSandboxBackend,
)

IMAGE = "python:3.12-slim"
CONTROL_PORT = 8787


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


@pytest.fixture
def backend() -> DockerSandboxBackend:
    return DockerSandboxBackend(image=IMAGE, control_port=CONTROL_PORT)


@pytest.fixture
async def sandbox(backend: DockerSandboxBackend):
    """创建容器并在任何结局下兜底 `docker rm -f`。"""
    handle = await backend.create()
    try:
        yield handle
    finally:
        subprocess.run(
            ["docker", "rm", "-f", handle.sandbox_id],
            capture_output=True,
            timeout=60,
        )


def _container_state(sandbox_id: str) -> str:
    done = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", sandbox_id],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return done.stdout.strip() if done.returncode == 0 else ""


async def test_create_then_exec_echoes(backend, sandbox):
    assert sandbox.backend == "docker"
    assert sandbox.sandbox_id

    returncode, stdout, stderr = await backend.exec(
        sandbox, ["sh", "-c", "echo $GREETING"], env={"GREETING": "hello-roost"}
    )

    assert (returncode, stdout.strip(), stderr) == (0, "hello-roost", "")


async def test_exec_reports_nonzero_and_stderr(backend, sandbox):
    returncode, stdout, stderr = await backend.exec(
        sandbox, ["sh", "-c", "echo oops >&2; exit 3"]
    )

    assert returncode == 3
    assert stdout == ""
    assert "oops" in stderr


async def test_exec_timeout_raises(backend, sandbox):
    with pytest.raises(SandboxTimeoutError):
        await backend.exec(sandbox, ["sleep", "30"], timeout_seconds=1.0)


async def test_upload_then_exec_reads_back(backend, sandbox):
    await backend.upload(
        sandbox,
        {
            "/srv/roost/hello.txt": b"uploaded-body\n",
            "/srv/roost/nested/deep.bin": bytes(range(256)),
        },
    )

    returncode, stdout, _ = await backend.exec(sandbox, ["cat", "/srv/roost/hello.txt"])
    assert (returncode, stdout) == (0, "uploaded-body\n")

    returncode, stdout, _ = await backend.exec(
        sandbox, ["wc", "-c", "/srv/roost/nested/deep.bin"]
    )
    assert returncode == 0
    assert stdout.split()[0] == "256"


async def test_request_reaches_detached_http_server(backend, sandbox):
    await backend.upload(sandbox, {"/srv/http/probe.txt": b"served-by-sandbox"})
    returncode, _, stderr = await backend.exec(
        sandbox,
        [
            "sh",
            "-c",
            f"nohup python -m http.server {CONTROL_PORT} --directory /srv/http "
            ">/dev/null 2>&1 &",
        ],
    )
    assert returncode == 0, stderr

    status, body = await _request_until_up(backend, sandbox, "/probe.txt")
    assert (status, body) == (200, b"served-by-sandbox")

    status, _ = await backend.request(sandbox, "GET", "/missing.txt", timeout_seconds=10)
    assert status == 404


async def test_pause_then_connect_resumes(backend, sandbox):
    await backend.pause(sandbox)
    assert _container_state(sandbox.sandbox_id) == "paused"

    reconnected = await backend.connect(sandbox.sandbox_id)

    assert reconnected == sandbox
    assert _container_state(sandbox.sandbox_id) == "running"
    returncode, stdout, _ = await backend.exec(reconnected, ["echo", "awake"])
    assert (returncode, stdout.strip()) == (0, "awake")


async def test_connect_unknown_sandbox_raises(backend):
    with pytest.raises(SandboxNotFoundError):
        await backend.connect(f"roost-missing-{uuid.uuid4().hex}")


async def test_kill_removes_container(backend):
    handle = await backend.create()
    try:
        assert _container_state(handle.sandbox_id) == "running"
        await backend.kill(handle)
        assert _container_state(handle.sandbox_id) == ""
    finally:
        subprocess.run(
            ["docker", "rm", "-f", handle.sandbox_id], capture_output=True, timeout=60
        )


async def test_created_container_carries_sandbox_label(backend, sandbox):
    done = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            f"{{{{index .Config.Labels \"{SANDBOX_LABEL_KEY}\"}}}}",
            sandbox.sandbox_id,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0
    assert f"{SANDBOX_LABEL_KEY}={done.stdout.strip()}" == SANDBOX_LABEL


async def _request_until_up(backend, handle, path: str, attempts: int = 60):
    """detached 服务需要一点起动时间；轮询到第一次成功响应。"""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return await backend.request(handle, "GET", path, timeout_seconds=5)
        except OSError as exc:  # URLError 是 OSError 子类
            last = exc
            await asyncio.sleep(0.25)
    raise AssertionError(f"sandbox http server never came up: {last!r}")
