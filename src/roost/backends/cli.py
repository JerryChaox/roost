"""docker CLI 调用边界。

本模块只做一件事：把 `docker …` 变成 (returncode, stdout, stderr)，并在超时时
终止子进程。它不知道沙箱语义，也不构造任何 docker 参数——参数由 backend 组装。
"""

from __future__ import annotations

import asyncio

from .errors import DockerCommandError, SandboxTimeoutError

__all__ = ["DockerCli"]


class DockerCli:
    """asyncio subprocess 包装的 docker CLI。"""

    def __init__(self, docker_bin: str = "docker") -> None:
        self._docker_bin = docker_bin

    @property
    def docker_bin(self) -> str:
        return self._docker_bin

    async def run(
        self,
        args: list[str],
        *,
        stdin: bytes | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, str, str]:
        """执行 docker 子命令，返回 (returncode, stdout, stderr)（文本已解码）。"""
        argv = [self._docker_bin, *args]
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                process.communicate(input=stdin), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            await self._terminate(process)
            raise SandboxTimeoutError(
                f"{' '.join(argv)} exceeded timeout of {timeout_seconds}s"
            ) from None
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        returncode = process.returncode if process.returncode is not None else -1
        return returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def run_checked(
        self,
        args: list[str],
        *,
        stdin: bytes | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        """执行并要求 returncode == 0，返回 stdout；否则 raise DockerCommandError。"""
        returncode, out, err = await self.run(
            args, stdin=stdin, timeout_seconds=timeout_seconds
        )
        if returncode != 0:
            raise DockerCommandError([self._docker_bin, *args], returncode, out, err)
        return out

    async def available(self) -> bool:
        """docker CLI 与 daemon 是否可用（测试与宿主探活用）。"""
        try:
            returncode, _, _ = await self.run(["version", "--format", "{{.Server.Version}}"])
        except (OSError, SandboxTimeoutError):
            return False
        return returncode == 0

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.kill()
        except ProcessLookupError:
            return
        try:
            await process.wait()
        except asyncio.CancelledError:
            raise
