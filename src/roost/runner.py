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
"""

from __future__ import annotations

import asyncio
import time

from .control.client import DEFAULT_WAIT_MS, ControlClient
from .events import DriverEvent, Terminal
from .ports import EventSink, OpsRecorder
from .reducer import reduce_events
from .sessions import SessionSandboxRegistry
from .types import TurnEnvelope

__all__ = ["SandboxTurnRunner", "TurnFailedError", "TurnStreamTimeoutError"]


class TurnFailedError(RuntimeError):
    """沙箱里的 turn 以 Terminal(status='error') 结束。"""

    def __init__(self, turn_id: str, error: str | None) -> None:
        super().__init__(f"turn {turn_id!r} 失败：{error or '未提供错误信息'}")
        self.turn_id = turn_id
        self.error = error


class TurnStreamTimeoutError(TimeoutError):
    """在 turn_timeout 内没有等到 Terminal。"""


class SandboxTurnRunner:
    """M1 pipeline 的 runner 实现：沙箱取得 → 提交 turn → 拉事件 → 送 sink。

    参数：
        registry:     session 到沙箱的绑定与 cold boot 编排。
        sink:         DisplayEvent 的去处（EventSink port）。
        ops:          fire-and-forget 观测（可选）。
        wait_ms:      单次长轮询的等待上限（毫秒）。
        turn_timeout: 等待 Terminal 的总时限（秒）；None = 不设限（hang 的
                      识别归 M5 watchdog，本层不重复造一套判定）。
    """

    def __init__(
        self,
        registry: SessionSandboxRegistry,
        sink: EventSink,
        *,
        ops: OpsRecorder | None = None,
        wait_ms: int = DEFAULT_WAIT_MS,
        turn_timeout: float | None = None,
    ) -> None:
        if wait_ms < 0:
            raise ValueError("wait_ms 必须 >= 0")
        self._registry = registry
        self._sink = sink
        self._ops = ops
        self._wait_ms = wait_ms
        self._turn_timeout = turn_timeout

    async def __call__(self, turn: TurnEnvelope) -> None:
        await self.run(turn)

    async def run(self, turn: TurnEnvelope) -> None:
        _, client = await self._registry.get_or_create(
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

        terminal = await self._drain(client, turn)
        if terminal.status != "ok":
            raise TurnFailedError(turn.turn_id, terminal.error)

    # -- 事件流 ---------------------------------------------------------

    async def _drain(self, client: ControlClient, turn: TurnEnvelope) -> Terminal:
        """长轮询拉事件直到 Terminal，逐批经 reducer 送 EventSink。"""
        started = time.monotonic()
        cursor = 0
        while True:
            page = await client.fetch_events(
                turn.turn_id, after=cursor, wait_ms=self._wait_ms
            )
            cursor = page.next_after
            if page.events:
                await self._sink.emit(
                    reduce_events(page.events, session_id=turn.session_id)
                )
            terminal = _terminal_of(page.events)
            if terminal is not None:
                return terminal
            if (
                self._turn_timeout is not None
                and time.monotonic() - started >= self._turn_timeout
            ):
                raise TurnStreamTimeoutError(
                    f"turn {turn.turn_id!r} 在 {self._turn_timeout}s 内没有终态"
                )
            # driver 已给过一次长轮询等待；这里不再额外 sleep（空转由 wait_ms 限速）。
            if self._wait_ms == 0:
                await asyncio.sleep(0)

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
