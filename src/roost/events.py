"""驱动器事件。

driver -> host 的 wire 事件与 reducer 输出。全部 frozen dataclass。
契约见 CONTRACTS.md《驱动器事件》一节。
"""

from dataclasses import dataclass
from typing import Any

__all__ = [
    "Delta",
    "ToolEvent",
    "LifecycleNotice",
    "Terminal",
    "DriverEvent",
    "DisplayEvent",
]


@dataclass(frozen=True)
class Delta:
    turn_id: str
    text: str
    seq: int


@dataclass(frozen=True)
class ToolEvent:
    turn_id: str
    name: str
    phase: str                        # "start" | "result"
    detail: dict[str, Any]
    seq: int


@dataclass(frozen=True)
class LifecycleNotice:
    turn_id: str
    kind: str                         # "boot_started" | "boot_finished" | "update_started" | "update_finished"
    elapsed_ms: int
    seq: int


@dataclass(frozen=True)
class Terminal:
    turn_id: str
    status: str                       # "ok" | "error"
    error: str | None
    usage: dict[str, Any]
    seq: int


DriverEvent = Delta | ToolEvent | LifecycleNotice | Terminal


@dataclass(frozen=True)
class DisplayEvent:
    """reducer 输出（骨架阶段仅占位定义）。"""

    session_id: str
    turn_id: str
    kind: str                         # "text" | "tool" | "lifecycle_notice" | "terminal"
    body: dict[str, Any]
    seq: int
