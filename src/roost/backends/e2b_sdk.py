"""E2B SDK 调用边界。

职责边界与 docker 版的 `cli.DockerCli` 同构：本模块只把 **E2B Python SDK 的调用**
包成一组窄方法，并把 SDK 的异常翻译成 roost 的 backend 错误。它不认识 driver、
不认识控制协议、不组装 URL——沙箱语义全部归 `e2b.E2BSandboxBackend`。

两条设计选择需要解释：

- **SDK 惰性 import**：核心零运行时依赖是硬约束（CONTRACTS.md 附录 J），所以
  `import e2b` 发生在第一次真正要用 SDK 的时候，未装 extra 时抛
  `MissingDependencyError` 并给出 `pip install "roost[e2b]"`。顶层 `import roost`
  因此在没装 extra 的环境里照常可用。
- **凭据解析顺序**：构造参数 `api_key` > 环境变量 `ROOST_E2B_API_KEY` > 交给 SDK
  自己解析（它读 `E2B_API_KEY`）。契约钉死的是前两级；第三级只是"不传就别拦着
  SDK 用它自己的约定"，因此 key 值永远不被本模块打印或转存。

核实依据（2026-08-17，e2b 2.39.1）：`AsyncSandbox.create(template=…, timeout=…,
network=…)`、`AsyncSandbox.connect(sandbox_id, timeout=…)`（暂停实例自动恢复）、
`sandbox.pause()`、`sandbox.kill()`、`sandbox.commands.run(cmd, background=…,
envs=…, timeout=…)`、`sandbox.files.write_files([WriteEntry(path, data)])`、
`sandbox.get_host(port) -> "{port}-{sandbox_id}.{domain}"`、
`sandbox.traffic_access_token`。
"""

from __future__ import annotations

import os
from typing import Any

from .errors import (
    BackendError,
    MissingDependencyError,
    SandboxNotFoundError,
    SandboxTimeoutError,
)

__all__ = [
    "ENV_API_KEY",
    "INSTALL_HINT",
    "SDK_ENV_API_KEY",
    "E2BSdk",
    "load_e2b",
]

ENV_API_KEY = "ROOST_E2B_API_KEY"       # 契约钉死的凭据环境变量
SDK_ENV_API_KEY = "E2B_API_KEY"         # SDK 自己的约定，仅作最后一级回退
INSTALL_HINT = 'pip install "roost[e2b]"'


def load_e2b() -> Any:
    """import 已安装的 e2b SDK；没装时抛可执行的安装指引。"""
    try:
        import e2b
    except ImportError as exc:  # pragma: no cover - 分支由单测经注入的假 importer 覆盖
        raise MissingDependencyError(
            f"E2BSandboxBackend 需要可选依赖 e2b，未安装。安装：{INSTALL_HINT}"
        ) from exc
    return e2b


class E2BSdk:
    """E2B SDK 的窄调用面。方法参数与返回值里的 `sandbox` 都是 SDK 的实例对象。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        sandbox_timeout: int | None = None,
        allow_public_traffic: bool = False,
        loader: Any = load_e2b,
    ) -> None:
        self._api_key = api_key
        self._sandbox_timeout = sandbox_timeout
        self._allow_public_traffic = allow_public_traffic
        self._loader = loader
        self._module: Any = None

    # -- SDK 装载与凭据 --------------------------------------------------

    @property
    def module(self) -> Any:
        """已装载的 e2b 模块（首次访问时才 import）。"""
        if self._module is None:
            self._module = self._loader()
        return self._module

    def ensure_loaded(self) -> None:
        """现在就装载 SDK。

        backend 实例化时调用：契约要的是"未装 extra 时**实例化**就报清晰错误"，
        而不是等到第一次 create 才在编排半途炸开。
        """
        _ = self.module

    def api_key(self) -> str | None:
        """解析出的 API key；None 表示"交给 SDK 自己解析"。绝不记录它的值。"""
        return self._api_key or os.environ.get(ENV_API_KEY) or None

    def _api_params(self) -> dict[str, Any]:
        key = self.api_key()
        return {"api_key": key} if key else {}

    # -- 生命周期 -------------------------------------------------------

    async def create(self, template: str | None) -> Any:
        module = self.module
        opts: dict[str, Any] = dict(self._api_params())
        if self._sandbox_timeout is not None:
            opts["timeout"] = self._sandbox_timeout
        if not self._allow_public_traffic:
            # 沙箱公开 URL 默认对任何知道它的人可达；控制面不该如此，
            # 因此默认要求 e2b-traffic-access-token（见 backend 模块 docstring）。
            opts["network"] = {"allow_public_traffic": False}
        with _translated(module, f"create sandbox from template {template!r}"):
            return await module.AsyncSandbox.create(template=template, **opts)

    async def connect(self, sandbox_id: str) -> Any:
        module = self.module
        opts: dict[str, Any] = dict(self._api_params())
        if self._sandbox_timeout is not None:
            opts["timeout"] = self._sandbox_timeout
        with _translated(module, f"connect to sandbox {sandbox_id!r}"):
            return await module.AsyncSandbox.connect(sandbox_id, **opts)

    async def pause(self, sandbox: Any) -> bool:
        with _translated(self.module, f"pause sandbox {sandbox.sandbox_id!r}"):
            return await sandbox.pause(**self._api_params())

    async def kill(self, sandbox: Any) -> bool:
        with _translated(self.module, f"kill sandbox {sandbox.sandbox_id!r}"):
            return await sandbox.kill(**self._api_params())

    # -- 数据与执行 -----------------------------------------------------

    async def write_files(self, sandbox: Any, files: dict[str, bytes]) -> None:
        """一次批量写入：整包 driver 源码是几十个文件，逐个往返太贵。"""
        module = self.module
        entries = [
            module.sandbox.filesystem.filesystem.WriteEntry(path=path, data=data)
            for path, data in files.items()
        ]
        with _translated(module, f"write {len(entries)} files"):
            await sandbox.files.write_files(entries)

    async def run(
        self,
        sandbox: Any,
        command: str,
        *,
        envs: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, str, str]:
        """执行一条 shell 命令，返回 (exit_code, stdout, stderr)。

        非零退出在 SDK 里是异常（`CommandExitException`），在 SandboxBackend port
        里是正常返回值——翻译在这里做，调用方看到的语义与 docker 版一致。
        """
        module = self.module
        try:
            result = await sandbox.commands.run(
                command, envs=envs or None, timeout=timeout_seconds
            )
        except module.CommandExitException as exc:
            return exc.exit_code, exc.stdout, exc.stderr
        except Exception as exc:  # noqa: BLE001 - 统一翻译成 backend 错误
            _raise_translated(module, exc, f"exec {command!r}")
        return result.exit_code, result.stdout, result.stderr

    # -- 端口代理 -------------------------------------------------------

    def origin(self, sandbox: Any, port: int) -> str:
        """沙箱端口的公开 HTTPS origin（SDK 的 `get_host` 只给 host，不带 scheme）。"""
        return f"https://{sandbox.get_host(port)}"

    @staticmethod
    def traffic_headers(sandbox: Any) -> dict[str, str]:
        """受限公开访问时必须带的 header；沙箱允许公开流量时为空。"""
        token = getattr(sandbox, "traffic_access_token", None)
        return {"e2b-traffic-access-token": token} if token else {}


class _translated:
    """把 SDK 异常翻译成 backend 错误的 context manager。"""

    def __init__(self, module: Any, what: str) -> None:
        self._module = module
        self._what = what

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is None or isinstance(exc, BackendError):
            return False
        _raise_translated(self._module, exc, self._what)
        return False  # pragma: no cover - _raise_translated 必然 raise


def _raise_translated(module: Any, exc: BaseException, what: str):
    """SDK 异常 → backend 错误。未识别的异常原样放行（不吞掉真实故障）。"""
    not_found = tuple(
        cls
        for name in ("SandboxNotFoundException", "NotFoundException")
        if (cls := getattr(module, name, None)) is not None
    )
    timeout = getattr(module, "TimeoutException", None)
    if not_found and isinstance(exc, not_found):
        raise SandboxNotFoundError(f"{what} failed: {exc}") from exc
    if timeout is not None and isinstance(exc, timeout):
        raise SandboxTimeoutError(f"{what} timed out: {exc}") from exc
    sandbox_exception = getattr(module, "SandboxException", None)
    if sandbox_exception is not None and isinstance(exc, sandbox_exception):
        raise BackendError(f"{what} failed: {exc}") from exc
    raise exc
