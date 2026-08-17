"""BackupCoordinator —— turn 边界上的工作区异步备份（I2 的写入侧）。

契约见 CONTRACTS.md《附录 G：M4 持久化整合契约》。本模块只承担一类职责：**在
turn 结束后，把沙箱工作区搬到 SnapshotStore 里去**，并保证这件事永远不会反过来
伤到 turn。

三条语义要点：

- **fire-and-forget 是语义而不是偷懒**（DESIGN.md I2：snapshot 写在 turn 边界
  异步进行，写失败不影响 turn 结果）。因此 `schedule()` 是同步方法、不 await、
  不抛异常：调用它的是 runner 的成功/失败收尾路径，那里已经有一个要交给宿主的
  答案了，备份没有资格改写它。失败只经 OpsRecorder 留痕。
- **同 session 并发去重**：同一个 session 上一次备份还没跑完时，本次直接跳过而
  不是排队。排队没有价值——后到的那次要备份的是同一个工作区的更晚状态，而更晚
  的状态在下一个 turn 结束时还会再备一次；堆积反而会把慢存储的压力放大成沙箱
  的压力。
- **`drain()` 是给测试与关停用的句柄**，不是热路径的一部分。热路径永远不等备份。

失败模式的边界：备份的原子性归 SnapshotStore（FileSnapshotStore 的临时文件 +
rename），本模块不重试——重试策略要么由宿主在 store 实现里做，要么就等下一个
turn 边界，这两者都比在这里造一套 backoff 更可解释。
"""

from __future__ import annotations

import asyncio

from .control import ControlClient
from .ports import OpsRecorder, SnapshotKeyFn, SnapshotStore

__all__ = ["BackupCoordinator"]


class BackupCoordinator:
    """把工作区备份到 SnapshotStore 的调度器。

    参数：
        store:    SnapshotStore port 实现（快照的去处）。
        key_fn:   session_id → 存储 key（宿主提供，库不解释其结构）。
        ops:      fire-and-forget 观测（可选）。
    """

    def __init__(
        self,
        store: SnapshotStore,
        key_fn: SnapshotKeyFn,
        *,
        ops: OpsRecorder | None = None,
    ) -> None:
        self._store = store
        self._key_fn = key_fn
        self._ops = ops
        self._running: dict[str, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule(self, session_id: str, client: ControlClient) -> None:
        """安排一次备份；已有同 session 备份在跑则跳过。绝不抛异常。"""
        if session_id in self._running:
            self._record("workspace_backup_skipped", session_id=session_id)
            return
        try:
            task = asyncio.get_running_loop().create_task(
                self._backup(session_id, client),
                name=f"roost-backup-{session_id}",
            )
        except RuntimeError:        # 没有运行中的事件循环——不该发生，也不该炸调用方
            self._record("workspace_backup_unscheduled", session_id=session_id)
            return
        self._running[session_id] = task
        self._tasks.add(task)
        task.add_done_callback(self._forget)

    async def drain(self) -> None:
        """等待所有在飞备份结束（测试与关停用；热路径不调用）。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
            await asyncio.sleep(0)      # 让 done callback 把集合清干净

    @property
    def pending(self) -> int:
        """在飞备份数（观测用）。"""
        return len(self._tasks)

    # -- 内部 -----------------------------------------------------------

    async def _backup(self, session_id: str, client: ControlClient) -> None:
        key = self._key_fn(session_id)
        try:
            data = await client.get_workspace()
            await self._store.put(key, data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— I2：备份失败绝不外溢
            self._record(
                "workspace_backup_failed",
                session_id=session_id,
                key=key,
                error=repr(exc),
            )
            return
        self._record(
            "workspace_backup_finished",
            session_id=session_id,
            key=key,
            bytes=len(data),
        )

    def _forget(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        for session_id, running in list(self._running.items()):
            if running is task:
                del self._running[session_id]

    def _record(self, event_type: str, **details: object) -> None:
        if self._ops is None:
            return
        try:
            self._ops.record(event_type, **details)
        except Exception:  # OpsRecorder 契约：绝不 raise
            pass
