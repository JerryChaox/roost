"""事件产生与缓存 —— seq 分配、per-turn 内存缓存、长轮询等待原语。

契约见 CONTRACTS.md《附录 B — Turn registry 语义 · 事件存储》：

- per-turn 内存列表，seq 自 1 单调递增；
- Terminal 恒为最后一条（本模块用 `append` 的显式拒绝把它变成不变量，
  而不是靠调用方自觉）；
- M2 不设内存上限（有界化随 M6，PROTOCOL.md 已注明）。

职责边界：只管"事件序列 + 有人在等新事件"这一件事。seq 由这里唯一分配——
harness 产出的事件 seq 由本模块覆盖，harness 不需要知道全局序。
长轮询的 cursor 语义（`after` -> `next_after`）也归这里，server 只做转译。

线程模型：绑定单个事件循环，`append` 必须在该循环所在线程调用（driver 全程
单循环单 worker，天然满足）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace

from ..events import DriverEvent, Terminal

__all__ = ["EventLog"]


class EventLog:
    """per-turn 事件缓存。"""

    def __init__(self) -> None:
        self._events: dict[str, list[DriverEvent]] = {}
        self._signals: dict[str, asyncio.Event] = {}
        self._terminated: set[str] = set()

    # ---- 写入端 -------------------------------------------------------------

    def open(self, turn_id: str) -> None:
        """为一个 turn 建立空缓存（重复 open 是 no-op：duplicate 提交不清空历史）。"""
        self._events.setdefault(turn_id, [])
        self._signals.setdefault(turn_id, asyncio.Event())

    def append(self, turn_id: str, event: DriverEvent) -> DriverEvent:
        """追加一个事件；本方法分配 seq（覆盖入参值）并唤醒长轮询等待者。"""
        if turn_id in self._terminated:
            raise ValueError(f"turn {turn_id!r} 已 Terminal，不得再追加事件")
        self.open(turn_id)
        events = self._events[turn_id]
        stamped = replace(event, seq=len(events) + 1)
        events.append(stamped)
        if isinstance(stamped, Terminal):
            self._terminated.add(turn_id)
        signal = self._signals[turn_id]
        signal.set()
        # 换一个新 Event：已醒的等待者会自行重扫缓存，后来的等待者从干净状态开始。
        self._signals[turn_id] = asyncio.Event()
        return stamped

    def emitter(self, turn_id: str) -> Callable[[DriverEvent], None]:
        """返回交给 harness 的 emit 回调（harness 只管产出，不管 seq 与缓存）。"""

        def emit(event: DriverEvent) -> None:
            self.append(turn_id, event)

        return emit

    def has_terminal(self, turn_id: str) -> bool:
        return turn_id in self._terminated

    # ---- 读取端 -------------------------------------------------------------

    def snapshot(self, turn_id: str) -> list[DriverEvent]:
        return list(self._events.get(turn_id, ()))

    async def read(
        self, turn_id: str, *, after: int, wait_ms: int
    ) -> tuple[list[DriverEvent], int]:
        """长轮询：返回 seq > after 的事件与新 cursor。

        无新事件时最多等待 wait_ms 再返回空列表。用同一 `after` 重复读天然幂等——
        这正是"host 侧 cursor"让事件拉取可重放的原因。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_ms / 1000
        while True:
            pending = [e for e in self._events.get(turn_id, ()) if e.seq > after]
            if pending:
                return pending, pending[-1].seq
            remaining = deadline - loop.time()
            if remaining <= 0:
                return [], after
            signal = self._signals.setdefault(turn_id, asyncio.Event())
            try:
                await asyncio.wait_for(signal.wait(), timeout=remaining)
            except TimeoutError:
                return [], after
