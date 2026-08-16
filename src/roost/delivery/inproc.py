"""InProcessTurnDelivery —— TurnDelivery port 的进程内实现。

契约见 CONTRACTS.md《宿主 ports》TurnDelivery 与《附录 A》交付模块一节。

投递语义是 **at-least-once**：本层不承诺去重，重复投递由库的幂等机制
（StateStore.begin_turn）吸收。因此这里刻意提供两个"制造重复"的能力：
- duplicate_factor：每次 enqueue 人为投递 N 份同一 envelope（测试注入点）；
- 消费失败重投：handler 抛异常时以 attempt+1 重新入队。

attempt 只用于观测，不参与幂等（TurnEnvelope.attempt 的契约注释）。
本模块只承担一类职责：队列与重投调度。turn 的状态语义在 pipeline.py。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timezone

from ..types import TurnEnvelope

__all__ = ["InProcessTurnDelivery"]

TurnHandler = Callable[[TurnEnvelope], Awaitable[None]]


class InProcessTurnDelivery:
    """asyncio 队列实现的 TurnDelivery。

    参数：
        duplicate_factor: 每次 enqueue 实际投递的份数（>1 用于人为制造重复投递）。
        max_attempts:     消费失败后允许的最大 attempt；达到上限不再重投，
                          envelope 落入 `dropped` 供宿主/测试检查。
        retry_delay:      失败重投前的延迟（秒）。
    """

    def __init__(
        self,
        *,
        duplicate_factor: int = 1,
        max_attempts: int = 5,
        retry_delay: float = 0.0,
    ) -> None:
        if duplicate_factor < 1:
            raise ValueError("duplicate_factor 必须 >= 1")
        if max_attempts < 1:
            raise ValueError("max_attempts 必须 >= 1")
        self._duplicate_factor = duplicate_factor
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay
        self._queue: asyncio.Queue[TurnEnvelope] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._timers: set[asyncio.Task[None]] = set()
        self._handler: TurnHandler | None = None
        self.dropped: list[TurnEnvelope] = []

    # ---- 生产端（TurnDelivery port） -----------------------------------------

    async def enqueue(
        self, turn: TurnEnvelope, *, not_before: datetime | None = None
    ) -> None:
        """at-least-once 投递；重复投递由库的幂等机制吸收，投递层不承诺去重。"""
        delay = self._delay_for(not_before)
        for _ in range(self._duplicate_factor):
            if delay <= 0:
                self._queue.put_nowait(turn)
            else:
                self._schedule(turn, delay)

    @staticmethod
    def _delay_for(not_before: datetime | None) -> float:
        if not_before is None:
            return 0.0
        moment = (
            not_before.replace(tzinfo=timezone.utc)
            if not_before.tzinfo is None
            else not_before
        )
        return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())

    def _schedule(self, turn: TurnEnvelope, delay: float) -> None:
        """到点入队的延迟任务；被跟踪以便 stop() 时能干净取消。"""

        async def _later() -> None:
            await asyncio.sleep(delay)
            self._queue.put_nowait(turn)

        task = asyncio.ensure_future(_later())
        self._timers.add(task)
        task.add_done_callback(self._timers.discard)

    # ---- 消费端 ---------------------------------------------------------------

    def start(self, handler: TurnHandler, *, concurrency: int = 1) -> None:
        """启动消费 worker。handler 抛异常时按 at-least-once 重投。"""
        if self._workers:
            raise RuntimeError("InProcessTurnDelivery 已启动")
        if concurrency < 1:
            raise ValueError("concurrency 必须 >= 1")
        self._handler = handler
        self._workers = [
            asyncio.ensure_future(self._worker()) for _ in range(concurrency)
        ]

    async def _worker(self) -> None:
        assert self._handler is not None
        while True:
            turn = await self._queue.get()
            settled = False
            try:
                await self._handler(turn)
            except asyncio.CancelledError:
                # worker 在关停途中被取消：把消息还回队列，避免静默丢件。
                # put 与 task_done 成对，join() 的未完成计数保持平衡。
                self._queue.put_nowait(turn)
                self._queue.task_done()
                settled = True
                raise
            except Exception:
                self._redeliver(turn)
            finally:
                if not settled:
                    self._queue.task_done()

    def _redeliver(self, turn: TurnEnvelope) -> None:
        """消费失败重投：attempt+1；超过 max_attempts 则丢弃并记录。"""
        if turn.attempt >= self._max_attempts:
            self.dropped.append(turn)
            return
        retried = replace(turn, attempt=turn.attempt + 1)
        if self._retry_delay <= 0:
            self._queue.put_nowait(retried)
        else:
            self._schedule(retried, self._retry_delay)

    async def join(self) -> None:
        """等待当前已入队的消息全部被消费完。"""
        await self._queue.join()

    async def stop(self) -> None:
        """优雅停机：取消延迟任务与 worker，并等待它们退出。"""
        for timer in list(self._timers):
            timer.cancel()
        if self._timers:
            await asyncio.gather(*list(self._timers), return_exceptions=True)
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        self._handler = None
