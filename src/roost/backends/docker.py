"""DockerSandboxBackend —— 本机 Docker 上的 SandboxBackend 实现（M3a）。

职责边界：本模块只组装沙箱语义（容器怎么起、handle 怎么来、端口怎么解析），
把 CLI 调用交给 `cli.DockerCli`、tar 构造交给 `archive.build_tar`、HTTP 通道
交给 `http.request_loopback`。契约见 CONTRACTS.md 附录 D。
"""

from __future__ import annotations

from ..protocol import DEFAULT_CONTROL_PORT
from ..types import SandboxHandle
from .archive import build_tar
from .cli import DockerCli
from .errors import SandboxNotFoundError
from .http import request_loopback

__all__ = [
    "BACKEND_NAME",
    "DEFAULT_CONTROL_PORT",
    "DEFAULT_IMAGE",
    "SANDBOX_LABEL",
    "SANDBOX_LABEL_KEY",
    "SANDBOX_LABEL_VALUE",
    "DockerSandboxBackend",
]

BACKEND_NAME = "docker"
DEFAULT_IMAGE = "python:3.12-slim"
# 控制端口的唯一定义在 `roost.protocol`（附录 F 的常量统一）；此处只把同一个名字
# 转出，既有导入路径 `roost.backends.DEFAULT_CONTROL_PORT` 保持可用。
SANDBOX_LABEL_KEY = "roost.sandbox"
SANDBOX_LABEL_VALUE = "1"
SANDBOX_LABEL = f"{SANDBOX_LABEL_KEY}={SANDBOX_LABEL_VALUE}"

_LOOPBACK = "127.0.0.1"


class DockerSandboxBackend:
    """SandboxBackend port 的 Docker 实现（shell 出 docker CLI，不引 docker SDK）。"""

    def __init__(
        self,
        *,
        image: str = DEFAULT_IMAGE,
        control_port: int = DEFAULT_CONTROL_PORT,
        docker_bin: str = "docker",
        cli: DockerCli | None = None,
    ) -> None:
        self._image = image
        self._control_port = control_port
        self._cli = cli if cli is not None else DockerCli(docker_bin)
        self._host_ports: dict[str, int] = {}

    @property
    def cli(self) -> DockerCli:
        return self._cli

    # -- 生命周期 -------------------------------------------------------

    async def create(self, *, template: str | None = None) -> SandboxHandle:
        """`docker run -d`：主进程 sleep infinity，控制端口发布到 127.0.0.1 临时端口。"""
        image = template or self._image
        stdout = await self._cli.run_checked(
            [
                "run",
                "-d",
                "--label",
                SANDBOX_LABEL,
                "-p",
                f"{_LOOPBACK}::{self._control_port}/tcp",
                image,
                "sleep",
                "infinity",
            ]
        )
        container_id = stdout.strip()
        if not container_id:
            raise SandboxNotFoundError(f"docker run returned no container id for {image!r}")
        return SandboxHandle(sandbox_id=container_id, backend=BACKEND_NAME)

    async def connect(self, sandbox_id: str) -> SandboxHandle:
        """连接既有容器；处于 paused 时隐含恢复（契约不设独立 resume）。"""
        state = await self._inspect_state(sandbox_id)
        if state == "paused":
            await self._cli.run_checked(["unpause", sandbox_id])
        return SandboxHandle(sandbox_id=sandbox_id, backend=BACKEND_NAME)

    async def pause(self, handle: SandboxHandle) -> None:
        await self._cli.run_checked(["pause", handle.sandbox_id])

    async def kill(self, handle: SandboxHandle) -> None:
        await self._cli.run_checked(["rm", "-f", handle.sandbox_id])
        self._host_ports.pop(handle.sandbox_id, None)

    # -- 数据与执行 -----------------------------------------------------

    async def upload(self, handle: SandboxHandle, files: dict[str, bytes]) -> None:
        """files（容器内绝对路径 → bytes）打成内存 tar，经 `docker cp -` 落盘。"""
        if not files:
            return
        tar_bytes = build_tar(files)
        await self._cli.run_checked(
            ["cp", "-", f"{handle.sandbox_id}:/"], stdin=tar_bytes
        )

    async def exec(
        self,
        handle: SandboxHandle,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, str, str]:
        args = ["exec"]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        args.append(handle.sandbox_id)
        args += list(argv)
        return await self._cli.run(args, timeout_seconds=timeout_seconds)

    async def request(
        self,
        handle: SandboxHandle,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, bytes]:
        """到沙箱内 driver loopback control server 的 HTTP 通道。"""
        port = await self._host_port(handle.sandbox_id)
        return await request_loopback(
            port,
            method,
            path,
            body=body,
            headers=headers,
            timeout_seconds=timeout_seconds,
        )

    # -- 内部 -----------------------------------------------------------

    async def _inspect_state(self, sandbox_id: str) -> str:
        returncode, stdout, stderr = await self._cli.run(
            ["inspect", "--format", "{{.State.Status}}", sandbox_id]
        )
        if returncode != 0:
            raise SandboxNotFoundError(
                f"container {sandbox_id!r} not found: {stderr.strip() or stdout.strip()}"
            )
        return stdout.strip()

    async def _host_port(self, sandbox_id: str) -> int:
        cached = self._host_ports.get(sandbox_id)
        if cached is not None:
            return cached
        stdout = await self._cli.run_checked(
            ["port", sandbox_id, f"{self._control_port}/tcp"]
        )
        port = _parse_published_port(stdout)
        if port is None:
            raise SandboxNotFoundError(
                f"container {sandbox_id!r} publishes no host port for "
                f"{self._control_port}/tcp"
            )
        self._host_ports[sandbox_id] = port
        return port


def _parse_published_port(output: str) -> int | None:
    """解析 `docker port` 输出，取 IPv4 loopback 映射（回退到任一行）。"""
    candidates = [line.strip() for line in output.splitlines() if line.strip()]
    if not candidates:
        return None
    for line in candidates:
        host, _, port = line.rpartition(":")
        if host.strip("[]") == _LOOPBACK and port.isdigit():
            return int(port)
    _, _, port = candidates[0].rpartition(":")
    return int(port) if port.isdigit() else None
