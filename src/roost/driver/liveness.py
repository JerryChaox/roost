"""driver 侧的 liveness 时钟 —— "这个进程上次做任何事是多久以前"。

契约见 CONTRACTS.md《附录 M — 双时钟》。pull 模型里没有"driver 主动上报心跳"
这回事，因此 liveness 以**元字段**的形式挂在 events 长轮询响应上：
`liveness_quiet_ms` = driver 自上次任意内部活动至今的毫秒数。

**在 driver 本地计算**是这个设计的全部要点：宿主与沙箱的墙钟可以差出任意值
（不同机器、不同时区、NTP 漂移），而一个"多久以前"是差值，跨机传输不需要
两端的时钟对齐。

什么算"活动"（收窄到 driver 自己能观察到的三类）：

- harness 产出任何事件（emit.py 的每一次 append）——SDK 消息、tool 事件、终态；
- turn 生命周期转换（提交入队、开始执行、执行完成）；
- harness 自身的心跳自检（有实现的话，经 `touch()` 报进来）。

**刻意不算活动**：宿主的轮询（GET /v1/health、GET .../events）。让被观测者因为
"有人在看"就显得活着，是这套判定里最容易犯的错——liveness 会永远新鲜，矩阵里
"liveness 竭"的三行永不可达，双时钟退化回单时钟。同理，本模块**没有**自我 touch
的定时器。
"""

from __future__ import annotations

import time
from collections.abc import Callable

__all__ = ["ActivityClock"]


class ActivityClock:
    """单调时钟上的"上次活动时刻"。

    参数：
        clock: 返回单调秒数的时钟（注入点只为测试；生产用 time.monotonic）。
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last = clock()

    def touch(self) -> None:
        """记一次内部活动。调用点应当只有"driver 自己做了什么"，见模块 docstring。"""
        self._last = self._clock()

    @property
    def quiet_seconds(self) -> float:
        return max(0.0, self._clock() - self._last)

    def quiet_ms(self) -> int:
        """安静时长（毫秒，向下取整）——events 响应里的 `liveness_quiet_ms`。"""
        return int(self.quiet_seconds * 1000)
