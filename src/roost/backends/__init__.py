"""SandboxBackend 实现。

M3a 交付本机 Docker backend，M7 交付可选依赖的 E2B backend。

`E2BSandboxBackend` / `E2BSdk` 走模块级 `__getattr__` **惰性导出**：`roost.backends.e2b`
本身不在 import 期碰 e2b SDK，但把它排除在包 import 之外还能多挡一层——没装
`roost[e2b]` extra 的用户 `import roost` 与用 docker backend 的路径，绝不因为
E2B 相关模块的存在付出任何代价。
"""

from typing import TYPE_CHECKING, Any

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
    MissingDependencyError,
    SandboxNotFoundError,
    SandboxTimeoutError,
)
from .http import request_loopback, request_url

if TYPE_CHECKING:  # 类型检查器要看得见真实符号，运行期仍然惰性
    from .e2b import E2BSandboxBackend
    from .e2b_sdk import E2BSdk

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
    "E2BSandboxBackend",
    "E2BSdk",
    "MissingDependencyError",
    "SandboxNotFoundError",
    "SandboxTimeoutError",
    "build_tar",
    "request_loopback",
    "request_url",
]

_LAZY = {"E2BSandboxBackend": ".e2b", "E2BSdk": ".e2b_sdk"}


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_name, __name__), name)


def __dir__() -> list[str]:
    return sorted(__all__)
