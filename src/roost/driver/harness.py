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
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ..events import Delta, DriverEvent, Terminal
from ..types import TurnEnvelope
from .workspace import workspace_dir_from_env

__all__ = ["Harness", "EchoHarness", "COUNTER_FILE"]

COUNTER_FILE = "counter"


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
      不变量由 driver 而不是 harness 保证）；
    - `payload["counter"]`：为真则递增工作区里的 `counter` 文件并把新值附在回显后
      （`… counter=<n>`）。这是 **Demo 2（持久化）的观测面**：counter 跨沙箱重生
      续增，才说明工作区真的被备份并恢复了。刻意做成 payload 开关而不是新协议
      字段/端点——协议面零增（附录 G）。
    """

    def __init__(
        self,
        *,
        chunk_size: int = 8,
        delay_ms: int = 0,
        fail: str | None = None,
        workspace_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size 必须 >= 1")
        self._chunk_size = chunk_size
        self._delay_ms = delay_ms
        self._fail = fail
        self._workspace_dir = workspace_dir

    async def run(self, turn: TurnEnvelope, emit: Callable[[DriverEvent], None]) -> None:
        payload = turn.payload
        text = _echo_text(payload)
        if payload.get("counter"):
            value = await asyncio.to_thread(self._bump_counter)
            text = f"{text} counter={value}"
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

    # -- counter（Demo 2 的持久化观测面） ---------------------------------

    def _bump_counter(self) -> int:
        """读-加一-写工作区里的 `counter`，返回新值。

        无锁、非原子：driver 里同一时刻只有一个 turn 在跑（单 FIFO worker），
        这里不需要也不该自己造一套并发控制。
        """
        root = (
            workspace_dir_from_env()
            if self._workspace_dir is None
            else Path(self._workspace_dir)
        )
        root.mkdir(parents=True, exist_ok=True)
        path = root / COUNTER_FILE
        try:
            value = int(path.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            value = 0
        value += 1
        path.write_text(f"{value}\n", encoding="utf-8")
        return value


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
