"""`python -m roost.driver` —— 沙箱内 driver 进程入口。

契约见 CONTRACTS.md《附录 B — M2 交付模块》：端口经 `ROOST_DRIVER_PORT`（默认 8787），
绑定 127.0.0.1（控制面只在 loopback 上，不对沙箱外暴露）。

M2 的 harness 是 `EchoHarness`；真实 Claude Agent SDK harness 归 M3，届时只换
这里注入的实现，server/worker/registry 无需改动。

就绪信号：绑定成功后向 stdout 打印一行
`roost-driver listening on <host>:<port>` 并 flush。端口传 0 时由内核分配，这一行
是调用方（测试/编排器）取得实际端口的唯一途径——避免"先探测空闲端口再启动"的竞态。
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys

from ..protocol import ENV_PREFIX
from .harness import EchoHarness
from .server import ControlServer

__all__ = ["main"]

ENV_PORT = f"{ENV_PREFIX}DRIVER_PORT"
DEFAULT_PORT = 8787
HOST = "127.0.0.1"


async def _serve(port: int) -> None:
    server = ControlServer(EchoHarness(), host=HOST, port=port)
    await server.start()
    print(f"roost-driver listening on {HOST}:{server.port}", flush=True)

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
    try:
        asyncio.run(_serve(port))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
