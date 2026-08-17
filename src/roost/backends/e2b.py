"""E2BSandboxBackend —— E2B 云沙箱上的 SandboxBackend 实现（M7）。

职责边界与 docker 版同构：本模块只组装**沙箱语义**（沙箱怎么起、handle 怎么来、
控制端口的 origin 怎么解析、argv 怎么变成一条命令），SDK 调用交给
`e2b_sdk.E2BSdk`、HTTP 通道交给 `http.request_url`。契约见 CONTRACTS.md 附录 J。

driver 绑定地址（附录 J 要求写进本 docstring 的结论）
--------------------------------------------------
**E2B 的端口代理可达沙箱内的 127.0.0.1，因此 driver 应按附录 F 增补传
`bind_host="127.0.0.1"`**（`DriverInstaller(bind_host="127.0.0.1")`）。

依据是实测而非推断：官方文档（Sandbox public URL）只说明每个沙箱端口有一个
`https://{port}-{sandbox_id}.{domain}` 的公开 URL，并未规定服务必须监听
0.0.0.0。2026-08-17 用 e2b 2.39.1 对真实沙箱实测：同一沙箱内分别起
`python3 -m http.server --bind 127.0.0.1` 与 `--bind 0.0.0.0`，两者经各自公开
URL 均返回 200——端口代理跑在沙箱内部，因此 loopback 服务同样可达。
`bind_host="0.0.0.0"`（docker 版的默认）在 E2B 上也能工作，两种都支持；选
127.0.0.1 是因为它把 driver 的监听面收到最小，暴露与否完全由 E2B 侧的端口代理
与下面的 traffic token 决定。

控制面的暴露面
--------------
沙箱公开 URL 默认对任何知道它的人可达，而 driver 的控制端口就是控制面。因此
本 backend **默认以 `allow_public_traffic=False` 创建沙箱**，并对每个 `request`
带上 `e2b-traffic-access-token`（实测：不带 → 403，带 → 200，token 在
pause→connect 后仍然有效）。宿主可以 `allow_public_traffic=True` 显式放开。

核实到的 SDK 面（e2b 2.39.1，2026-08-17）见 `e2b_sdk` 模块 docstring。
"""

from __future__ import annotations

import shlex

from ..protocol import DEFAULT_CONTROL_PORT
from ..types import SandboxHandle
from .e2b_sdk import E2BSdk
from .errors import SandboxNotFoundError
from .http import request_url

__all__ = [
    "BACKEND_NAME",
    "DEFAULT_BIND_HOST",
    "E2BSandboxBackend",
]

BACKEND_NAME = "e2b"
# 本 backend 建议给 DriverInstaller 的 bind_host（结论见模块 docstring）。
DEFAULT_BIND_HOST = "127.0.0.1"


class E2BSandboxBackend:
    """SandboxBackend port 的 E2B 实现（惰性依赖 e2b SDK）。

    参数：
        template:             E2B template id；None 用 E2B 默认 base 模板。
        control_port:         driver 在沙箱内监听的端口（协议常量）。
        api_key:              E2B API key；省略时读环境变量 `ROOST_E2B_API_KEY`，
                              再省略则交给 SDK 自己解析（`E2B_API_KEY`）。
        sandbox_timeout:      E2B 侧的沙箱存活时限（秒）。None 用 SDK 默认（300s）。
                              create 与 connect 都会带上它——connect 只会延长，
                              不会缩短已有时限。
        allow_public_traffic: True 时沙箱公开 URL 对所有人可达；默认 False，
                              请求带 `e2b-traffic-access-token`。
        sdk:                  注入的 SDK 调用面（测试用）。
    """

    def __init__(
        self,
        *,
        template: str | None = None,
        control_port: int = DEFAULT_CONTROL_PORT,
        api_key: str | None = None,
        sandbox_timeout: int | None = None,
        allow_public_traffic: bool = False,
        sdk: E2BSdk | None = None,
    ) -> None:
        self._template = template
        self._control_port = control_port
        self._sdk = sdk if sdk is not None else E2BSdk(
            api_key=api_key,
            sandbox_timeout=sandbox_timeout,
            allow_public_traffic=allow_public_traffic,
        )
        # 没装 extra 要在**这里**就报错（附录 J），而不是等到编排半途 create 时。
        self._sdk.ensure_loaded()
        # sandbox_id → SDK 沙箱对象。exec/upload/request 都要一个活的 SDK 对象，
        # 而 handle 只有 id（port 契约如此）——缓存未命中就 connect 补上。
        self._sandboxes: dict[str, object] = {}

    @property
    def sdk(self) -> E2BSdk:
        return self._sdk

    @property
    def control_port(self) -> int:
        return self._control_port

    # -- 生命周期 -------------------------------------------------------

    async def create(self, *, template: str | None = None) -> SandboxHandle:
        sandbox = await self._sdk.create(template if template is not None else self._template)
        sandbox_id = sandbox.sandbox_id
        if not sandbox_id:
            raise SandboxNotFoundError("E2B create returned a sandbox without an id")
        self._sandboxes[sandbox_id] = sandbox
        return SandboxHandle(sandbox_id=sandbox_id, backend=BACKEND_NAME)

    async def connect(self, sandbox_id: str) -> SandboxHandle:
        """连接既有沙箱；处于暂停态时由 E2B 隐含恢复（契约不设独立 resume）。"""
        sandbox = await self._sdk.connect(sandbox_id)
        self._sandboxes[sandbox_id] = sandbox
        return SandboxHandle(sandbox_id=sandbox_id, backend=BACKEND_NAME)

    async def pause(self, handle: SandboxHandle) -> None:
        sandbox = await self._sandbox(handle.sandbox_id)
        await self._sdk.pause(sandbox)
        # 暂停后缓存里的对象不再能执行任何操作；丢弃它，任何后续操作都会重新
        # connect —— 而 connect 隐含恢复，正是契约要的语义。
        self._sandboxes.pop(handle.sandbox_id, None)

    async def kill(self, handle: SandboxHandle) -> None:
        sandbox = await self._sandbox(handle.sandbox_id)
        try:
            await self._sdk.kill(sandbox)
        finally:
            self._sandboxes.pop(handle.sandbox_id, None)

    # -- 数据与执行 -----------------------------------------------------

    async def upload(self, handle: SandboxHandle, files: dict[str, bytes]) -> None:
        """files（沙箱内绝对路径 → bytes）一次批量写入。"""
        if not files:
            return
        sandbox = await self._sandbox(handle.sandbox_id)
        await self._sdk.write_files(sandbox, files)

    async def exec(
        self,
        handle: SandboxHandle,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, str, str]:
        """argv 经 shlex 拼成一条命令交给 E2B（SDK 的 run 只收字符串）。

        env / timeout / 非零退出的语义与 docker 版对齐：env 注入进程环境，
        超时抛 SandboxTimeoutError，非零退出是正常返回值而不是异常。
        """
        sandbox = await self._sandbox(handle.sandbox_id)
        return await self._sdk.run(
            sandbox,
            shlex.join(argv),
            envs=env,
            timeout_seconds=timeout_seconds,
        )

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
        """到沙箱内 driver control server 的 HTTP 通道（经 E2B 端口代理走 HTTPS）。"""
        sandbox = await self._sandbox(handle.sandbox_id)
        merged = dict(headers or {})
        merged.update(self._sdk.traffic_headers(sandbox))
        return await request_url(
            self._sdk.origin(sandbox, self._control_port),
            method,
            path,
            body=body,
            headers=merged,
            timeout_seconds=timeout_seconds,
        )

    # -- 内部 -----------------------------------------------------------

    async def _sandbox(self, sandbox_id: str):
        """取一个可用的 SDK 沙箱对象；未缓存则 connect（暂停实例隐含恢复）。"""
        cached = self._sandboxes.get(sandbox_id)
        if cached is not None:
            return cached
        sandbox = await self._sdk.connect(sandbox_id)
        self._sandboxes[sandbox_id] = sandbox
        return sandbox
