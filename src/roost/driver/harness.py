"""Harness port 与 M2 的测试用实现 EchoHarness。

契约见 CONTRACTS.md《附录 B — Harness 接口》。真实 Claude Agent SDK harness 归 M3；
本模块此刻只钉死"driver 如何驱动一个 agent"的接口形状：

- `run` 经 `emit` 产出事件，**实现负责在结束前 emit Terminal**；
- `run` 抛异常时由 worker 兜底 emit `Terminal(status="error")`。

emit 是同步回调而非 await：事件写入是纯内存操作（emit.py），让 harness 侧
无需感知事件缓存与长轮询，也不会因为上报把自己 block 住。

事件里的 `seq` 由 emit.py 统一分配，harness 构造事件时填 0 即可。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, Protocol

from ..events import Delta, DriverEvent, Terminal
from ..types import TurnEnvelope

__all__ = ["Harness", "EchoHarness"]


class Harness(Protocol):
    async def run(self, turn: TurnEnvelope, emit: Callable[[DriverEvent], None]) -> None:
        """执行一个 turn，经 emit 产出事件；实现负责在结束前 emit Terminal。

        run 抛异常时由 worker 兜底 emit Terminal(status='error')。
        """


class EchoHarness:
    """把 payload 回显成若干 Delta + 一条 Terminal 的测试 harness。

    行为可经构造参数设默认，也可**由 payload 逐 turn 覆盖**——子进程端到端测试
    因此无需额外的注入通道（协议面不为测试增加任何字段/端点）：

    - `payload["text"]`：回显文本（缺省时回显 payload 的 JSON 形式）；
    - `payload["delay_ms"]`：每个 Delta 之前的延迟毫秒数；
    - `payload["fail"]`：非空则在回显后抛 RuntimeError（验证 worker 的 Terminal 兜底）；
    - `payload["skip_terminal"]`：为真则不 emit Terminal（验证"Terminal 恒为最后一条"
      不变量由 driver 而不是 harness 保证）。
    """

    def __init__(
        self,
        *,
        chunk_size: int = 8,
        delay_ms: int = 0,
        fail: str | None = None,
    ) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size 必须 >= 1")
        self._chunk_size = chunk_size
        self._delay_ms = delay_ms
        self._fail = fail

    async def run(self, turn: TurnEnvelope, emit: Callable[[DriverEvent], None]) -> None:
        payload = turn.payload
        text = _echo_text(payload)
        delay_ms = _int_option(payload, "delay_ms", self._delay_ms)
        fail = payload.get("fail") or self._fail

        for start in range(0, max(len(text), 1), self._chunk_size):
            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000)
            emit(Delta(turn_id=turn.turn_id, text=text[start : start + self._chunk_size], seq=0))

        if fail:
            raise RuntimeError(str(fail))
        if payload.get("skip_terminal"):
            return
        emit(
            Terminal(
                turn_id=turn.turn_id,
                status="ok",
                error=None,
                usage={"echoed_chars": len(text)},
                seq=0,
            )
        )


def _echo_text(payload: dict[str, Any]) -> str:
    text = payload.get("text")
    if isinstance(text, str):
        return text
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _int_option(payload: dict[str, Any], name: str, default: int) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value
