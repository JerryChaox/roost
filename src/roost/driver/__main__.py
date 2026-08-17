"""`python -m roost.driver` —— 沙箱内 driver 进程入口。

契约见 CONTRACTS.md《附录 B — M2 交付模块》：端口经 `ROOST_DRIVER_PORT`（默认 8787），
**默认**绑定 127.0.0.1（控制面只在 loopback 上，不对沙箱外暴露）。

绑定地址可经 `ROOST_DRIVER_HOST` 覆盖，默认不变。这个开关是 M3b 集成时被迫加的：
docker 的端口发布把宿主端口转发到**容器网卡**，只监听容器内 loopback 的服务因此
从宿主完全不可达（附录 F 的启动命令里据此设 0.0.0.0）。暴露面并没有因此变宽——
沙箱边界仍在，backend 只把端口发布到宿主的 127.0.0.1。E2B 一类自带代理的 backend
维持默认的 loopback 绑定即可。

工作区目录经 `ROOST_WORKSPACE_DIR`（默认 `~/workspace`，启动时 expanduser 解析：
root 下是 /root/workspace，E2B 一类非 root 沙箱下是 /home/user/workspace），启动时
确保存在——它是
`/v1/workspace` 备份/恢复的对象（附录 G）。创建失败只告警不退出：driver 的职责是
接 turn，"这台机器不给建目录"不该让协议面整个起不来。

harness 经 `ROOST_HARNESS`（`module:attr` 工厂）选择，缺省
`roost.driver.harness:EchoHarness`；真实 Claude Agent SDK harness 是
`roost.harness_claude:ClaudeHarness`（附录 K）。工厂在绑定端口**之前**执行，
导入或实例化失败 → 进程退出码 3，宿主按 boot 失败处理；server/worker/registry
对换了哪个 harness 一无所知。

就绪信号：绑定成功后向 stdout 打印一行
`roost-driver listening on <host>:<port>` 并 flush。端口传 0 时由内核分配，这一行
是调用方（测试/编排器）取得实际端口的唯一途径——避免"先探测空闲端口再启动"的竞态。
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys

from ..protocol import DEFAULT_CONTROL_PORT, ENV_PREFIX
from .harness import HarnessLoadError, harness_from_env
from .server import ControlServer
from .workspace import ensure_workspace_dir, workspace_dir_from_env

__all__ = ["main"]

ENV_PORT = f"{ENV_PREFIX}DRIVER_PORT"
ENV_HOST = f"{ENV_PREFIX}DRIVER_HOST"
DEFAULT_PORT = DEFAULT_CONTROL_PORT
HOST = "127.0.0.1"


async def _serve(port: int, host: str, harness) -> None:
    workspace = workspace_dir_from_env()
    workspace_warning: str | None = None
    try:
        ensure_workspace_dir(workspace)
    except OSError as exc:
        # 创建不了就继续起服务，只是 workspace 端点会报错。driver 的存在理由是接
        # turn，不是管目录；把"没有工作区"变成"driver 起不来"会让一台不该持久化的
        # 机器（比如只读根的开发机）连协议面都起不来。
        workspace_warning = f"warning: 无法创建工作区目录 {workspace}：{exc!r}"

    server = ControlServer(harness, host=host, port=port, workspace_dir=workspace)
    await server.start()
    print(f"roost-driver listening on {host}:{server.port}", flush=True)
    # 告警压在就绪行之后：**输出的第一行是就绪行**是协议承诺（PROTOCOL.md §5），
    # 调用方靠它取端口，任何抢在它前面的诊断输出都会被当成就绪行。
    if workspace_warning is not None:
        print(workspace_warning, file=sys.stderr, flush=True)

    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except (NotImplementedError, ValueError):   # 非 POSIX / 非主线程
            pass

    serving = asyncio.create_task(server.serve_forever(), name="roost-driver-http")
    stop = asyncio.create_task(stopping.wait(), name="roost-driver-stop")
    try:
        await asyncio.wait({serving, stop}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # 顺序要紧：先 close()（取消在飞连接 + 关监听），serve_forever 才会立刻结束。
        # 反过来先 cancel(serve_forever) 会走进 asyncio 自带的
        # close() + wait_closed() 收尾，而 wait_closed 要等在飞连接自然结束——
        # 一个挂着的长轮询就能把 SIGTERM 的关停拖满 wait_ms。
        await server.close()
        for task in (serving, stop):
            task.cancel()
        for task in (serving, stop):
            try:
                await task
            except asyncio.CancelledError:
                pass


def main(argv: list[str] | None = None) -> int:
    del argv                                    # 配置只走环境变量，无命令行参数面
    try:
        port = int(os.environ.get(ENV_PORT, str(DEFAULT_PORT)))
    except ValueError:
        print(f"{ENV_PORT} 必须是整数", file=sys.stderr, flush=True)
        return 2
    # harness 在**绑定端口之前**造好：造不出来就根本不该有一个能接 turn 的
    # 控制面（附录 K：不可导入/实例化失败 → driver 启动失败）。
    try:
        harness = harness_from_env()
    except HarnessLoadError as exc:
        print(f"harness 加载失败：{exc}", file=sys.stderr, flush=True)
        return 3
    try:
        asyncio.run(_serve(port, os.environ.get(ENV_HOST, HOST), harness))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
