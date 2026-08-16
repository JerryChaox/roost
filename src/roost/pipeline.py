"""TurnProcessor —— 投递消费端的 turn 状态机。

契约见 CONTRACTS.md《附录 A：M1 内核契约 — M1 交付模块与内部接口》。
本模块只承担一类职责：把一个 TurnEnvelope 走完
`session 串行门 → 幂等门 → runner → 收尾` 这条状态流；
存储 SQL 在 store/，队列与重投在 delivery/，二者都只经 port 面调用。

流程（逐字对应附录 A）：

1. `has_active_turn(排除自身)` 为真 → 排队等待（简单重投延后），本次不进 runner。
2. `begin_turn` 为 False → 丢弃（sender 侧幂等：同一 turn_id 已被别处认领或已完成）。
3. True → runner，其间以 lock_seconds/2 为周期 renew_turn_lock 心跳。
4. 收尾 `finish_turn('finished' / 'failed')`。

M1 串行化边界：串行门（has_active_turn）与幂等门（begin_turn）是两次独立的
CAS，二者之间没有 session 级互斥。因此"同一 session 的两个**不同** turn 恰好
并发消费"时可能同时越过串行门——单消费者（delivery concurrency=1，默认）下
不可达；多 worker 场景的 session 互斥由后续里程碑的 advisory lock 承接。

两条刻意的非对称（DESIGN.md 不变量 I1）：
- runner 抛异常是**终态**：记 'failed' 并吞掉异常，不走投递重投；恢复只由
  sweep → requeue → 重投递这一条路径负责，避免两条恢复路径互相打架。
- `process` 自身被 cancel（宿主崩溃/关停）时**不吞 CancelledError、也不 finish_turn**：
  让锁自然过期，交给 sweep 认领。这正是 wedged turn 能被恢复的前提。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from .ports import StateStore, TurnDelivery
from .types import TurnEnvelope

__all__ = ["TurnProcessor"]

TurnRunner = Callable[[TurnEnvelope], Awaitable[None]]

STATUS_FINISHED = "finished"
STATUS_FAILED = "failed"


class TurnProcessor:
    """消费一个 turn 的处理器。

    参数：
        store:            source of truth（StateStore port）。
        runner:           实际执行 turn 的协程（M1 用 fake，M3 起接 sandbox 链路）。
        delivery:         有则把"session 忙"的 turn 重新投递延后处理；
                          无则原地 sleep 后重试串行门。
        lock_seconds:     turn 锁时长；心跳周期为其一半。
        busy_retry_delay: 串行门未通过时的延后秒数。
    """

    def __init__(
        self,
        store: StateStore,
        runner: TurnRunner,
        *,
        delivery: TurnDelivery | None = None,
        lock_seconds: int = 30,
        busy_retry_delay: float = 0.05,
    ) -> None:
        if lock_seconds < 1:
            raise ValueError("lock_seconds 必须 >= 1")
        self._store = store
        self._runner = runner
        self._delivery = delivery
        self._lock_seconds = lock_seconds
        self._busy_retry_delay = busy_retry_delay

    async def process(self, turn: TurnEnvelope) -> None:
        """处理单个 turn。正常返回表示本次投递已被消化（执行、延后或丢弃）。"""
        if not await self._pass_serial_gate(turn):
            return

        # 幂等门：只有这里返回 True 的那一次投递才会真正执行 runner。
        if not await self._store.begin_turn(turn, lock_seconds=self._lock_seconds):
            return

        heartbeat = asyncio.ensure_future(self._heartbeat(turn.turn_id))
        try:
            try:
                await self._runner(turn)
            except asyncio.CancelledError:
                # 宿主崩溃/关停：不收尾，让锁过期后由 sweep 认领。
                raise
            except Exception:
                await self._store.finish_turn(turn.turn_id, status=STATUS_FAILED)
            else:
                await self._store.finish_turn(turn.turn_id, status=STATUS_FINISHED)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    async def _pass_serial_gate(self, turn: TurnEnvelope) -> bool:
        """session 串行门：同一 session 同时只允许一个活跃 turn。

        返回 True 表示可以继续；False 表示本次投递已被延后处理。
        """
        while await self._store.has_active_turn(
            turn.session_id, exclude_turn_id=turn.turn_id
        ):
            if self._delivery is not None:
                not_before = datetime.now(timezone.utc) + timedelta(
                    seconds=self._busy_retry_delay
                )
                await self._delivery.enqueue(turn, not_before=not_before)
                return False
            await asyncio.sleep(self._busy_retry_delay)
        return True

    async def _heartbeat(self, turn_id: str) -> None:
        """runner 执行期间按 lock_seconds/2 续锁，直到被取消。"""
        interval = self._lock_seconds / 2
        while True:
            await asyncio.sleep(interval)
            await self._store.renew_turn_lock(turn_id, lock_seconds=self._lock_seconds)
