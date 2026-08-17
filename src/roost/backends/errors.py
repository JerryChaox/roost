"""backend 错误类型。

单独成模块：CLI 包装、tar 构造、HTTP 通道三类职责都要引用同一组错误，
放在任何一个职责模块里都会制造反向依赖。
"""

from __future__ import annotations

__all__ = [
    "BackendError",
    "DockerCommandError",
    "MissingDependencyError",
    "SandboxNotFoundError",
    "SandboxTimeoutError",
]


class BackendError(RuntimeError):
    """backend 层所有错误的基类。"""


class DockerCommandError(BackendError):
    """docker CLI 以非零码退出。"""

    def __init__(self, argv: list[str], returncode: int, stdout: str, stderr: str) -> None:
        self.argv = list(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"{' '.join(argv)} exited with {returncode}: {stderr.strip() or stdout.strip()}"
        )


class MissingDependencyError(BackendError, ImportError):
    """backend 需要的可选依赖没装。

    核心零运行时依赖是硬约束，因此可选 backend 的第三方 SDK 只在实例化时才 import；
    没装时给出可执行的安装指引，而不是让 ImportError 从库深处漏出来。
    """


class SandboxNotFoundError(BackendError):
    """目标容器不存在。"""


class SandboxTimeoutError(BackendError, TimeoutError):
    """操作超过 timeout_seconds；底层进程已被终止。"""
