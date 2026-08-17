"""执行循环 —— 单 harness worker，FIFO，异常兜底。

契约见 CONTRACTS.md《附录 B — Turn registry 语义 · 执行序 / Harness 接口》：

- 单 worker、FIFO：driver 内天然串行，不存在并发 turn（宿主侧还有 session 串行门，
  两处串行是有意的冗余：driver 不信任上游一定守序）。
- harness 抛异常时 worker 兜底 emit `Terminal(status="error")`——**turn 永远有终态**，
  否则宿主的长轮询会一直等到 watchdog 超时，把一次可解释的失败变成一次 hang。
- harness 正常返回但没 emit Terminal，同样兜底：`Terminal 恒为最后一条`是 driver
  的不变量，不是 harness 的自觉。

职责边界：只管"取一个 turn、跑它、给它一个终态"。路由/编解码在 server.py，
条目状态在 registry.py，事件序与缓存在 emit.py。
"""

from __future__ import annotations

import asyncio

from ..events import Terminal
from ..types import TurnEnvelope
from .emit import EventLog
from .harness import Harness
from .registry import TurnRegistry

__all__ = ["TurnWorker"]

STATUS_OK = "ok"
STATUS_ERROR = "error"


class TurnWorker:
    """把 registry 已接受的 turn 串行喂给 harness。"""

    def __init__(
        self, harness: Harness, registry: TurnRegistry, events: EventLog
    ) -> None:
        self._harness = harness
        self._registry = registry
        self._events = events
        self._queue: asyncio.Queue[TurnEnvelope] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    # ---- 生命周期 -----------------------------------------------------------

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="roost-driver-worker")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @property
    def ready(self) -> bool:
        """worker 循环在跑 = harness 可接活（/v1/health 的 harness_ready）。"""
        return self._task is not None and not self._task.done()

    # ---- 入队与执行 ---------------------------------------------------------

    def submit(self, turn: TurnEnvelope) -> None:
        """排入 FIFO。仅由 server 在 registry 接受（非 duplicate）后调用。"""
        self._events.open(turn.turn_id)
        self._queue.put_nowait(turn)

    async def _loop(self) -> None:
        while True:
            turn = await self._queue.get()
            await self._run_one(turn)

    async def _run_one(self, turn: TurnEnvelope) -> None:
        self._registry.mark_running(turn.turn_id)
        emit = self._events.emitter(turn.turn_id)
        try:
            await self._harness.run(turn, emit)
        except asyncio.CancelledError:
            # 进程关停：不写终态，条目停在 running。跨进程恢复是宿主 watchdog 的
            # 决策（requeue 到新沙箱），driver 不假装自己能续上。
            raise
        except Exception as exc:  # noqa: BLE001 —— 兜底是本方法的存在理由
            self._ensure_terminal(turn, error=f"{type(exc).__name__}: {exc}")
        else:
            self._ensure_terminal(turn, error="harness 结束时未 emit Terminal")
        self._registry.mark_done(turn.turn_id, status=self._terminal_status(turn.turn_id))

    def _ensure_terminal(self, turn: TurnEnvelope, *, error: str) -> None:
        """harness 已给出 Terminal 则尊重它；否则补一条 error 终态。"""
        if self._events.has_terminal(turn.turn_id):
            return
        self._events.append(
            turn.turn_id,
            Terminal(
                turn_id=turn.turn_id, status=STATUS_ERROR, error=error, usage={}, seq=0
            ),
        )

    def _terminal_status(self, turn_id: str) -> str:
        events = self._events.snapshot(turn_id)
        last = events[-1] if events else None
        return last.status if isinstance(last, Terminal) else STATUS_ERROR
