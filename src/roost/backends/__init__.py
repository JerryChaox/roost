"""SandboxBackend 实现。

M3a 交付本机 Docker backend；E2B backend 归 M7。包级导出到 `roost` 顶层由集成
里程碑统一处理（附录 D 边界）。
"""

from .archive import build_tar
from .cli import DockerCli
from .docker import (
    BACKEND_NAME,
    DEFAULT_CONTROL_PORT,
    DEFAULT_IMAGE,
    SANDBOX_LABEL,
    SANDBOX_LABEL_KEY,
    SANDBOX_LABEL_VALUE,
    DockerSandboxBackend,
)
from .errors import (
    BackendError,
    DockerCommandError,
    SandboxNotFoundError,
    SandboxTimeoutError,
)
from .http import request_loopback

__all__ = [
    "BACKEND_NAME",
    "DEFAULT_CONTROL_PORT",
    "DEFAULT_IMAGE",
    "SANDBOX_LABEL",
    "SANDBOX_LABEL_KEY",
    "SANDBOX_LABEL_VALUE",
    "BackendError",
    "DockerCli",
    "DockerCommandError",
    "DockerSandboxBackend",
    "SandboxNotFoundError",
    "SandboxTimeoutError",
    "build_tar",
    "request_loopback",
]
