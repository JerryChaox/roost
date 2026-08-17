"""`python -m roost.driver.stop` —— 从沙箱内部把 driver 进程停掉。

契约见 CONTRACTS.md《附录 M — 升级阶梯》ordinal 1：**restart**——经 exec 重启
沙箱内的 driver 进程，工作区现场保留（比冷启动便宜得多）。restart 因此是两步：
先跑本模块把老进程停掉，再跑 `DriverInstaller.start_command()` 起新的。

为什么是一个 python 模块而不是 `pkill -f`：pkill 属 procps，精简镜像里经常没有
（python:3.12-slim 就没有），而 driver 唯一能保证存在的东西就是那个跑着它自己的
python。识别规则与 probe.py 共用同一个事实来源（cmdline 里有一个参数恰好等于
`roost.driver`），因此"哪个进程是 driver"只有一处定义。

停止是 SIGTERM 优先、超时 SIGKILL 兜底：driver 的信号处理会关监听、取消在飞的
长轮询连接（httpd.close），干净退出让端口立刻可重绑；一个卡在不可中断状态里的
driver 则必须被 SIGKILL，否则紧接着的 start 会撞 EADDRINUSE，把一次 restart
变成一次假成功。

输出单行 JSON（停了哪些 pid、用了哪个信号），退出码恒为 0：宿主要的是"现在没有
driver 在跑"这个状态，而"本来就没有在跑"同样满足它。
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

from .probe import DRIVER_ARG

__all__ = ["stop_drivers", "main", "TERM_GRACE_SECONDS"]

#: SIGTERM 之后等多久再 SIGKILL。driver 的关停路径只需要取消在飞连接，毫秒级；
#: 留 3 秒是给"正在写工作区的 harness"一点收尾余地，再多就是在拖慢恢复。
TERM_GRACE_SECONDS = 3.0

_POLL_INTERVAL = 0.05


def _driver_pids(proc_root: Path, *, exclude: int) -> list[int]:
    pids: list[int] = []
    try:
        entries = sorted(proc_root.iterdir(), key=lambda path: path.name)
    except OSError:
        return pids
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == exclude:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if DRIVER_ARG in raw.decode("utf-8", "replace").split("\0"):
            pids.append(pid)
    return pids


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_drivers(
    proc_root: str | os.PathLike[str] = "/proc",
    *,
    grace_seconds: float = TERM_GRACE_SECONDS,
) -> dict:
    """停掉全部 driver 进程，返回一份可 JSON 化的结果。"""
    root = Path(proc_root)
    pids = _driver_pids(root, exclude=os.getpid())
    termed: list[int] = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
        termed.append(pid)

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline and any(_alive(pid) for pid in termed):
        time.sleep(_POLL_INTERVAL)

    killed: list[int] = []
    for pid in termed:
        if not _alive(pid):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue
        killed.append(pid)
    return {"stopped": termed, "killed": killed}


def main(argv: list[str] | None = None) -> int:
    del argv                                    # 无参数面
    print(json.dumps(stop_drivers(), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
