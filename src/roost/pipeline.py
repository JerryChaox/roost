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

session 临界区（附录 L，关闭附录 A 的 M1 串行化边界）：串行门
（has_active_turn）与幂等门（begin_turn）是两次独立的 CAS，二者之间若无
session 级互斥，"同一 session 的两个**不同** turn 恰好并发消费"时会同时越过
串行门。因此这两步一起放进 `store.session_critical(session_id)`——串行门通过后
必须在同一临界区内完成 begin_turn 才释放。

临界区的持有范围严格等于这段复合判定（毫秒级），**绝不覆盖 runner 执行期**：
runner 一跑就是分钟级，锁若延伸到那里，跨实例的 Postgres advisory lock 会把
连接和 session 一起钉死。串行门未通过时的等待/重投也在临界区**之外**完成。

两条刻意的非对称（DESIGN.md 不变量 I1）：
- runner 抛异常是**终态**：记 'failed' 并吞掉异常，不走投递重投；恢复只由
  sweep → requeue → 重投递这一条路径负责，避免两条恢复路径互相打架。
- `process` 自身被 cancel（宿主崩溃/关停）时**不吞 CancelledError、也不 finish_turn**：
  让锁自然过期，交给 sweep 认领。这正是 wedged turn 能被恢复的前提。
- `TurnStalledError`（沙箱卡死，runner 已销毁沙箱）与 cancel 同类：**不收尾、
  不记 failed**，锁自然过期后交给 sweep → watchdog requeue。区别只在它正常返回
  而不外抛——外抛会触发投递层的消费失败重投，凭空多出第二条恢复路径（附录 H）。
- M12 的另外两种终结（`TurnAbandonedError` = 阶梯触顶不再重投、
  `TurnNeedsAttentionError` = wall-clock 触顶需人工介入）**不需要在这里专案**：
  runner 在抛之前已经把行写成 'failed' / 'attention'，而 finish_turn 只作用于
  running 行——下面兜底的那次 finish_turn('failed') 因此是一次静默 no-op，
  终态由做出决定的那一方写死，不会被这里覆盖成别的东西。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from .ports import StateStore, TurnDelivery
from .runner import TurnStalledError
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
        while True:
            async with self._store.session_critical(turn.session_id):
                if not await self._store.has_active_turn(
                    turn.session_id, exclude_turn_id=turn.turn_id
                ):
                    # 串行门通过。幂等门必须在同一临界区内完成：只有这里返回 True
                    # 的那一次投递才会真正执行 runner。
                    if not await self._store.begin_turn(
                        turn, lock_seconds=self._lock_seconds
                    ):
                        return
                    break
            # 串行门未通过。等待/重投一律在临界区之外做，绝不占着 session 锁睡觉。
            if not await self._defer(turn):
                return

        heartbeat = asyncio.ensure_future(self._heartbeat(turn.turn_id))
        try:
            try:
                await self._runner(turn)
            except asyncio.CancelledError:
                # 宿主崩溃/关停：不收尾，让锁过期后由 sweep 认领。
                raise
            except TurnStalledError:
                # 沙箱卡死且已被 runner 销毁：**既不收尾也不记 failed**。
                # 正常返回（而不是外抛）是关键——外抛会让投递层按消费失败重投，
                # 那就是第二条恢复路径。心跳随本函数返回而停，锁在 lock_seconds
                # 内自然过期，sweep → watchdog requeue 是唯一的接管者。
                return
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

    async def _defer(self, turn: TurnEnvelope) -> bool:
        """串行门未通过时的延后处理（在临界区之外调用）。

        返回 True 表示本任务应原地重试串行门；False 表示本次投递已交还投递层。
        """
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
