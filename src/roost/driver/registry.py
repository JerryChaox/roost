"""Turn registry —— I1 的 driver 侧幂等，纯内存状态机，零 IO。

契约见 CONTRACTS.md《附录 B — Turn registry 语义》：

- 进程内 dict，键 turn_id；**生命周期与 driver 进程绑定**——这不是缺陷而是
  I1 双侧分工的边界线：registry 只保证"同一沙箱进程内绝不重跑"，跨进程的重跑
  合法性由宿主 watchdog 决策（requeue 到新沙箱 = 新进程 = 空 registry）。
- 条目状态机 `queued → running → done(status)`。
- **duplicate 在任何状态下都只读返回**：重复 POST 绝不产生第二次执行，也绝不
  改写既有条目（包括不覆盖 payload/attempt——先到的那份 envelope 是权威）。

本模块只承担一类职责：条目状态。不认识 HTTP、不认识事件、不认识 harness，
因此可以脱离进程完整单测。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..types import TurnEnvelope

__all__ = [
    "STATE_QUEUED",
    "STATE_RUNNING",
    "STATE_DONE",
    "TurnEntry",
    "SubmitResult",
    "TurnRegistry",
    "UnknownTurn",
    "InvalidTransition",
]

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"


class UnknownTurn(LookupError):
    """registry 里没有这个 turn_id。"""


class InvalidTransition(RuntimeError):
    """状态机不允许的转换（driver 内部 bug，不是协议输入错误）。"""


@dataclass(frozen=True)
class TurnEntry:
    """registry 条目。`status` 仅在 done 状态有值（"ok" | "error"）。"""

    turn: TurnEnvelope
    state: str
    status: str | None = None

    @property
    def turn_id(self) -> str:
        return self.turn.turn_id


@dataclass(frozen=True)
class SubmitResult:
    """`accepted=False` 即 duplicate：`entry` 是既有条目的现态。"""

    entry: TurnEntry
    accepted: bool


class TurnRegistry:
    """turn_id -> TurnEntry 的状态机。单线程/单事件循环内使用。"""

    def __init__(self) -> None:
        self._entries: dict[str, TurnEntry] = {}

    def submit(self, turn: TurnEnvelope) -> SubmitResult:
        """登记一个新 turn。已见过该 turn_id 时只读返回现态，绝不改写、绝不重排队。"""
        existing = self._entries.get(turn.turn_id)
        if existing is not None:
            return SubmitResult(entry=existing, accepted=False)
        entry = TurnEntry(turn=turn, state=STATE_QUEUED)
        self._entries[turn.turn_id] = entry
        return SubmitResult(entry=entry, accepted=True)

    def get(self, turn_id: str) -> TurnEntry | None:
        return self._entries.get(turn_id)

    def mark_running(self, turn_id: str) -> TurnEntry:
        return self._transition(turn_id, expected=STATE_QUEUED, state=STATE_RUNNING)

    def mark_done(self, turn_id: str, *, status: str) -> TurnEntry:
        return self._transition(
            turn_id, expected=STATE_RUNNING, state=STATE_DONE, status=status
        )

    def turn_ids(self) -> list[str]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def _transition(
        self, turn_id: str, *, expected: str, state: str, status: str | None = None
    ) -> TurnEntry:
        entry = self._entries.get(turn_id)
        if entry is None:
            raise UnknownTurn(turn_id)
        if entry.state != expected:
            raise InvalidTransition(
                f"turn {turn_id!r} 处于 {entry.state!r}，无法转入 {state!r}"
            )
        updated = TurnEntry(turn=entry.turn, state=state, status=status)
        self._entries[turn_id] = updated
        return updated
