"""Watchdog —— 把锁过期的 turn 交还给投递层。

契约见 CONTRACTS.md《附录 H：M5 watchdog 与 liveness 契约》。本模块只承担一类
职责：周期性 `sweep_due_turns(limit)`，把扫出来的 envelope 以 attempt+1 重投。
它**不判定卡死**（那在 runner.py 的事件流停滞检测里）、**不碰沙箱**、
**不改 turn 行状态**（转 requeued 是 sweep 自己在单事务里完成的）。

三条语义要点：

- **attempt+1 的所有者是投递方**（附录 A）：`sweep_due_turns` 返回行内原值，
  这里 `replace(turn, attempt=turn.attempt + 1)` 后再 enqueue。存储层不自增，
  否则"扫出来但没投出去"就会把 attempt 白白吃掉一格。
- **只有这一条恢复路径**（I1/I3）：宿主崩溃、沙箱卡死、进程被杀——三种现实的
  共同表现都是"锁不再续期"，因此都由这里接管，重投后经正常投递 → begin_turn
  接管 requeued 行 → cold boot 新沙箱重跑。库里不存在第二个重跑发起者。
- **循环绝不因一次失败而死**：store/delivery 抛异常只记 ops 并等下一拍。
  watchdog 停摆意味着所有卡住的会话永久失声，比重复扫一次贵得多。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from .ports import OpsRecorder, StateStore, TurnDelivery
from .types import TurnEnvelope

__all__ = ["Watchdog", "DEFAULT_INTERVAL", "DEFAULT_SWEEP_LIMIT"]

DEFAULT_INTERVAL = 5.0
DEFAULT_SWEEP_LIMIT = 50


class Watchdog:
    """后台 sweep → requeue 循环。

    参数：
        store:       source of truth（StateStore port）。
        delivery:    重投去处（TurnDelivery port）。
        interval:    两次 sweep 之间的间隔（秒）。
        sweep_limit: 单次 sweep 的行数上限。
        ops:         fire-and-forget 观测（可选）。
    """

    def __init__(
        self,
        store: StateStore,
        delivery: TurnDelivery,
        *,
        interval: float = DEFAULT_INTERVAL,
        sweep_limit: int = DEFAULT_SWEEP_LIMIT,
        ops: OpsRecorder | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval 必须 > 0")
        if sweep_limit < 1:
            raise ValueError("sweep_limit 必须 >= 1")
        self._store = store
        self._delivery = delivery
        self._interval = interval
        self._sweep_limit = sweep_limit
        self._ops = ops
        self._task: asyncio.Task[None] | None = None

    # -- 生命周期 -------------------------------------------------------

    def start(self) -> None:
        """启动后台循环（与既有后台任务模式一致：同步 start / 异步 stop）。"""
        if self._task is not None:
            raise RuntimeError("Watchdog 已启动")
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        """取消后台循环并等它退出；未启动时是空操作。"""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # -- 一次 sweep ------------------------------------------------------

    async def sweep_once(self) -> list[TurnEnvelope]:
        """扫一批到期 turn 并逐个 attempt+1 重投，返回**重投出去**的信封。

        单独暴露是为了让宿主与测试能在不起后台任务的情况下走同一段逻辑——
        循环体与它之间不该存在第二份实现。
        """
        swept = await self._store.sweep_due_turns(limit=self._sweep_limit)
        requeued: list[TurnEnvelope] = []
        for turn in swept:
            retried = replace(turn, attempt=turn.attempt + 1)
            try:
                await self._delivery.enqueue(retried)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # sweep 已把这一行转成 requeued（单事务，不可撤销），投递却没成功：
                # 这一个 turn 就此掉队。逐个隔离至少不让它带走同一批里的其余行，
                # 并留下 ops 痕迹——批量放弃会把一次失败放大成一整批失声。
                self._record(
                    "watchdog_requeue_failed",
                    turn_id=turn.turn_id,
                    error=repr(exc),
                )
                continue
            requeued.append(retried)
        if requeued:
            self._record(
                "watchdog_requeued",
                turn_ids=[turn.turn_id for turn in requeued],
                count=len(requeued),
            )
        return requeued

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record("watchdog_sweep_failed", error=repr(exc))

    def _record(self, event_type: str, **details: object) -> None:
        if self._ops is None:
            return
        try:
            self._ops.record(event_type, **details)
        except Exception:  # OpsRecorder 契约：绝不 raise
            pass
