"""事件 reducer —— DriverEvent → DisplayEvent 的纯函数。

契约见 CONTRACTS.md《附录 F — 交付模块》。无 IO、无状态：一个 DriverEvent 加上
调用方补入的 session_id（和可选的 display seq）唯一决定一个 DisplayEvent。
把它单独成模块的理由是可测性——渲染语义的回归不需要起沙箱。

**seq 的分配约定**：同一个 turn 的 display 流里，boot/update 这类库自身产出的
lifecycle 通告排在 driver 事件之前，而 driver 侧 seq 从 1 起算（附录 B）。为了让
两段拼起来仍然单调递增，display 流为 lifecycle 通告保留 `1..LIFECYCLE_SEQ_RESERVED`
这一段，driver 事件统一平移到该段之后（`driver_display_seq`）。seq 只要求递增、
不要求连续，因此保留段留空是允许的；这样两边都无需共享可变状态，重复拉取同一
cursor 得到的 display seq 也依然稳定（幂等）。
"""

from __future__ import annotations

from dataclasses import asdict

from .events import Delta, DisplayEvent, DriverEvent, LifecycleNotice, Terminal, ToolEvent

__all__ = [
    "KIND_TEXT",
    "KIND_TOOL",
    "KIND_LIFECYCLE_NOTICE",
    "KIND_TERMINAL",
    "LIFECYCLE_SEQ_RESERVED",
    "driver_display_seq",
    "reduce_event",
    "reduce_events",
]

KIND_TEXT = "text"
KIND_TOOL = "tool"
KIND_LIFECYCLE_NOTICE = "lifecycle_notice"
KIND_TERMINAL = "terminal"

# display 流里为库自身的 lifecycle 通告保留的 seq 段（见模块 docstring）。
LIFECYCLE_SEQ_RESERVED = 16

_KINDS: dict[type, str] = {
    Delta: KIND_TEXT,
    ToolEvent: KIND_TOOL,
    LifecycleNotice: KIND_LIFECYCLE_NOTICE,
    Terminal: KIND_TERMINAL,
}

# turn_id / seq 已是 DisplayEvent 的顶层字段，不再重复进 body。
_PROMOTED_FIELDS = ("turn_id", "seq")


def driver_display_seq(seq: int) -> int:
    """driver 侧 seq（从 1 起）→ display 流 seq（跳过 lifecycle 保留段）。"""
    return LIFECYCLE_SEQ_RESERVED + seq


def reduce_event(
    event: DriverEvent, *, session_id: str, seq: int | None = None
) -> DisplayEvent:
    """把一个 driver 事件翻译成 DisplayEvent。

    `seq` 缺省沿用源事件的 seq；调用方需要把多段事件拼成单调流时显式覆盖。
    """
    kind = _KINDS.get(type(event))
    if kind is None:
        raise TypeError(f"未知的 driver 事件类型 {type(event).__name__}")
    body = {k: v for k, v in asdict(event).items() if k not in _PROMOTED_FIELDS}
    return DisplayEvent(
        session_id=session_id,
        turn_id=event.turn_id,
        kind=kind,
        body=body,
        seq=event.seq if seq is None else seq,
    )


def reduce_events(
    events: list[DriverEvent], *, session_id: str, offset: bool = True
) -> list[DisplayEvent]:
    """批量翻译 driver 事件；`offset` 为真时平移到 lifecycle 保留段之后。"""
    return [
        reduce_event(
            event,
            session_id=session_id,
            seq=driver_display_seq(event.seq) if offset else None,
        )
        for event in events
    ]
