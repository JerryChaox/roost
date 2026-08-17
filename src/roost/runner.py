"""SandboxTurnRunner —— 把一个 turn 送进沙箱并把事件流送回宿主。

契约见 CONTRACTS.md《附录 F — 交付模块》。它实现 M1 `TurnProcessor` 的 runner
签名（`async (TurnEnvelope) -> None`），因此在 pipeline 眼里只有两种结局：正常
返回 = `finish_turn('finished')`，抛异常 = `finish_turn('failed')`。

三条语义要点：

- **duplicate 不是错误**：driver 的 registry 已见过这个 turn_id 就绝不重跑
  （I1 driver 侧）。宿主这一侧要做的是照常拉流——沙箱里那次执行的事件流才是
  这个 turn 的答案。反过来说，"提交返回 duplicate 就直接返回"会丢掉终态。
- **拉流靠 cursor 长轮询**：`after` 是幂等的读位置（附录 B），重复读同一段不会
  产生第二次执行，也不会漏事件；循环的唯一出口是收到 Terminal。
- **Terminal(status='error') → raise**：turn 在沙箱里失败是 turn 的终态失败，
  交给 pipeline 记 'failed'；恢复只走 sweep → requeue → 新沙箱这一条路径。
- **备份挂在 Terminal 之后、raise 之前**（I2）：ok 与 error 都备。失败的 turn
  同样在工作区里留下了痕迹（写了一半的文件、日志），下一个沙箱要接着它干活；
  "只备份成功的 turn" 会让失败重试从一个更旧的世界开始。调度本身 fire-and-forget，
  不改变本方法的返回/抛出行为。
- **停滞（hang）判定在这一层，恢复不在**（附录 H / I3）：事件流在 `stall_timeout`
  内颗粒无收 → 销毁沙箱 + raise `TurnStalledError`，此后本进程对这个 turn 不再有
  任何动作。重跑由 pipeline 放手（不收尾）→ 锁自然过期 → watchdog sweep → requeue
  完成，本模块**不自己重投、不自己重连**。

停滞计时以"含事件的响应页"为准：空长轮询页不重置计时，慢而活着的 harness
（事件间隔 < stall_timeout）永不被误杀。M4 期的 `turn_timeout`（等 Terminal 的
总时限）已在 M5 并入本判定并删除——总时长上限会按 wall clock 砍掉一个正在稳定
出事件的长 turn，与 I3 的"idle 不误杀"直接冲突，而它触发时又只是普通 raise
（pipeline 记 failed），等于给卡死留了第二条含糊的出口。停滞恢复只有一条路径。
"""

from __future__ import annotations

import asyncio
import time
from typing import NoReturn

from .backup import BackupCoordinator
from .control.client import DEFAULT_WAIT_MS, ControlClient
from .events import DriverEvent, Terminal
from .ports import EventSink, OpsRecorder
from .reducer import reduce_events
from .sessions import SessionSandboxRegistry
from .types import SandboxHandle, TurnEnvelope

__all__ = [
    "SandboxTurnRunner",
    "TurnFailedError",
    "TurnStalledError",
    "DEFAULT_STALL_TIMEOUT",
]

DEFAULT_STALL_TIMEOUT = 60.0


class TurnFailedError(RuntimeError):
    """沙箱里的 turn 以 Terminal(status='error') 结束。"""

    def __init__(self, turn_id: str, error: str | None) -> None:
        super().__init__(f"turn {turn_id!r} 失败：{error or '未提供错误信息'}")
        self.turn_id = turn_id
        self.error = error


class TurnStalledError(TimeoutError):
    """turn 已提交，但事件流在 `stall_timeout` 内颗粒无收（附录 H 的 hang）。

    抛出时沙箱**已被销毁**，绑定行仍指向那个死沙箱（下一次 get_or_create 的
    health 探测自然走 cold boot）。pipeline 见到它既不收尾也不记 failed：锁在
    lock_seconds 内自然过期，watchdog 的 sweep → requeue 是唯一的恢复路径。
    """

    def __init__(self, turn_id: str, stall_timeout: float) -> None:
        super().__init__(f"turn {turn_id!r} 的事件流停滞超过 {stall_timeout}s")
        self.turn_id = turn_id
        self.stall_timeout = stall_timeout


class SandboxTurnRunner:
    """M1 pipeline 的 runner 实现：沙箱取得 → 提交 turn → 拉事件 → 送 sink。

    参数：
        registry:     session 到沙箱的绑定与 cold boot 编排。
        sink:         DisplayEvent 的去处（EventSink port）。
        backup:       turn 边界的工作区备份调度器（可选；None = 不备份）。
        ops:          fire-and-forget 观测（可选）。
        wait_ms:      单次长轮询的等待上限（毫秒）。
        stall_timeout: 距上一个**含事件**的响应页多久算停滞（秒）。必须显著大于
                      事件的自然间隔；与 lock_seconds 无耦合（锁由 heartbeat 维持，
                      只在 process 退出后才会过期）。
    """

    def __init__(
        self,
        registry: SessionSandboxRegistry,
        sink: EventSink,
        *,
        backup: BackupCoordinator | None = None,
        ops: OpsRecorder | None = None,
        wait_ms: int = DEFAULT_WAIT_MS,
        stall_timeout: float = DEFAULT_STALL_TIMEOUT,
    ) -> None:
        if wait_ms < 0:
            raise ValueError("wait_ms 必须 >= 0")
        if stall_timeout <= 0:
            raise ValueError("stall_timeout 必须 > 0")
        self._registry = registry
        self._sink = sink
        self._backup = backup
        self._ops = ops
        self._wait_ms = wait_ms
        self._stall_timeout = stall_timeout

    async def __call__(self, turn: TurnEnvelope) -> None:
        await self.run(turn)

    async def run(self, turn: TurnEnvelope) -> None:
        handle, client = await self._registry.get_or_create(
            turn.session_id, turn_id=turn.turn_id
        )

        submission = await client.submit_turn(turn)
        self._record(
            "turn_submitted",
            turn_id=turn.turn_id,
            session_id=turn.session_id,
            state=submission.state,
            turn_state=submission.turn_state,
        )

        terminal = await self._drain(client, turn, handle)
        if self._backup is not None:
            self._backup.schedule(turn.session_id, client)
        if terminal.status != "ok":
            raise TurnFailedError(turn.turn_id, terminal.error)

    # -- 事件流 ---------------------------------------------------------

    async def _drain(
        self, client: ControlClient, turn: TurnEnvelope, handle: SandboxHandle
    ) -> Terminal:
        """长轮询拉事件直到 Terminal，逐批经 reducer 送 EventSink。

        每一页的等待上限取 `min(wait_ms, 剩余停滞预算)`：判定的时间粒度因此由
        stall_timeout 决定，而不是被 wait_ms 拖成"最坏 wait_ms + stall_timeout"。
        """
        cursor = 0
        last_event_at = time.monotonic()
        while True:
            remaining = self._stall_timeout - (time.monotonic() - last_event_at)
            if remaining <= 0:
                await self._stalled(turn, handle)
            wait_ms = min(self._wait_ms, int(remaining * 1000))

            page = await client.fetch_events(
                turn.turn_id, after=cursor, wait_ms=wait_ms
            )
            cursor = page.next_after
            if page.events:
                # 只有"含事件的响应页"重置停滞计时；空长轮询页不算活着。
                last_event_at = time.monotonic()
                await self._sink.emit(
                    reduce_events(page.events, session_id=turn.session_id)
                )
                terminal = _terminal_of(page.events)
                if terminal is not None:
                    return terminal
            # driver 已给过一次长轮询等待；这里不再额外 sleep（空转由 wait_ms 限速）。
            if wait_ms == 0:
                await asyncio.sleep(0)

    async def _stalled(self, turn: TurnEnvelope, handle: SandboxHandle) -> NoReturn:
        """停滞收场：销毁沙箱 → raise。恒不返回。

        刻意**不备份**工作区：要备份就得再向这个已经不响应的 driver 发一次请求，
        那正是刚刚判定为不可用的通道；停滞 turn 的工作区回到上一次成功 turn 的
        快照，是比"卡在半路又拖住恢复"更好的起点。
        """
        await self._registry.destroy(handle)
        self._record(
            "sandbox_stalled_killed",
            turn_id=turn.turn_id,
            session_id=turn.session_id,
            sandbox_id=handle.sandbox_id,
            stall_timeout=self._stall_timeout,
        )
        raise TurnStalledError(turn.turn_id, self._stall_timeout)

    def _record(self, event_type: str, **details: object) -> None:
        if self._ops is None:
            return
        try:
            self._ops.record(event_type, **details)
        except Exception:  # OpsRecorder 契约：绝不 raise
            pass


def _terminal_of(events: list[DriverEvent]) -> Terminal | None:
    """取本批里的 Terminal（driver 不变量：恒为整个流的最后一条）。"""
    for event in events:
        if isinstance(event, Terminal):
            return event
    return None
