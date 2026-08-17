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
import importlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ..events import Delta, DriverEvent, Terminal
from ..protocol import ENV_PREFIX
from ..types import TurnEnvelope
from .workspace import workspace_dir_from_env

__all__ = [
    "Harness",
    "EchoHarness",
    "COUNTER_FILE",
    "load_harness",
    "harness_from_env",
    "HarnessLoadError",
    "ENV_HARNESS",
    "DEFAULT_HARNESS",
]

COUNTER_FILE = "counter"

#: driver 启动读它选 harness，形如 `module:attr`（附录 K）。
ENV_HARNESS = f"{ENV_PREFIX}HARNESS"
DEFAULT_HARNESS = "roost.driver.harness:EchoHarness"


class HarnessLoadError(RuntimeError):
    """`ROOST_HARNESS` 指向的东西导不进来或造不出来。

    driver 启动因此失败（宿主按 boot 失败处理）——一个"harness 没接上"的
    driver 还能应答 /v1/turn 才是更坏的结果：turn 会被接受、然后无声地全部
    走进兜底错误，看起来像模型在失败。
    """


def load_harness(spec: str) -> Harness:
    """`module:attr` → harness 实例（`attr` 无参可调用即可：类或工厂函数）。

    刻意不接受构造参数：沙箱里的配置面只有环境变量，harness 自己去读它需要的
    那几个（例如 ClaudeHarness 的 ROOST_CLAUDE_*）。把参数编进 spec 会让
    driver 变成一个小型配置语言的解释器。
    """
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise HarnessLoadError(f"{ENV_HARNESS} 必须形如 'module:attr'，收到 {spec!r}")
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 —— 导入期的任何失败都是启动失败
        raise HarnessLoadError(f"导入 {module_name!r} 失败：{exc!r}") from exc
    try:
        factory = getattr(module, attribute)
    except AttributeError as exc:
        raise HarnessLoadError(f"{module_name!r} 里没有 {attribute!r}") from exc
    try:
        return factory()
    except Exception as exc:  # noqa: BLE001 —— 实例化失败同样是启动失败
        raise HarnessLoadError(f"实例化 {spec!r} 失败：{exc!r}") from exc


def harness_from_env(env: dict[str, str] | None = None) -> Harness:
    """按 `ROOST_HARNESS` 造 harness，缺省仍是 EchoHarness。"""
    source = os.environ if env is None else env
    return load_harness(source.get(ENV_HARNESS) or DEFAULT_HARNESS)


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
    - `payload["hang_on_attempt"]`：等于 `turn.attempt` 时**不产出任何事件并永久
      挂起**。这是 **M5（watchdog 与 liveness）的观测面**：宿主侧的停滞检测只能
      靠"事件流颗粒无收"来判定，因此注入点必须是一个真的什么都不发的 harness。
      同样刻意做成 payload 开关而不是新协议字段/端点——协议面零增（附录 H）；
      按 attempt 触发是为了让"第一次挂死、requeue 后的第二次正常答复"能在同一个
      turn_id 上表达出来；
    - `payload["busy_child"]`：为真则在回显期间挂一个真的子进程（阻塞在管道读上）。
      这是 **M12（活性探测）的观测面**：它让"慢而活"在 `/proc` 上真的看起来像
      活着——只靠 `asyncio.sleep` 的慢 turn 与一个挂死的 turn 在内核眼里是同一
      个样子（driver 都睡在 epoll 上），那样就没法验证矩阵第三行与第四行的分岔；
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
        if _int_option(payload, "hang_on_attempt", 0) == turn.attempt:
            # 一个事件都不发，然后永不返回：宿主侧的 stall_timeout 是唯一的出口。
            await asyncio.Event().wait()
        text = _echo_text(payload)
        if payload.get("counter"):
            value = await asyncio.to_thread(self._bump_counter)
            text = f"{text} counter={value}"
        delay_ms = _int_option(payload, "delay_ms", self._delay_ms)
        fail = payload.get("fail") or self._fail

        child = await _busy_child() if payload.get("busy_child") else None
        try:
            for start in range(0, max(len(text), 1), self._chunk_size):
                if delay_ms > 0:
                    await asyncio.sleep(delay_ms / 1000)
                emit(
                    Delta(
                        turn_id=turn.turn_id,
                        text=text[start : start + self._chunk_size],
                        seq=0,
                    )
                )
        finally:
            await _reap(child)

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


async def _busy_child() -> asyncio.subprocess.Process | None:
    """起一个阻塞在管道读上的子进程（`cat` 等一个永远不来的 stdin）。

    它不烧 CPU，但在 `/proc` 里是一个货真价实的、在等外部输入的后代进程——
    这正是活性探测要认出来的"慢而活"。起不来就算了（探测那边会退回到"只有
    driver 在睡"，测试自己会失败得很清楚，不该在这里把 turn 也带崩）。
    """
    try:
        return await asyncio.create_subprocess_exec(
            "sh", "-c", "cat",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None


async def _reap(child: "asyncio.subprocess.Process | None") -> None:
    if child is None:
        return
    try:
        child.kill()
    except ProcessLookupError:
        pass
    await child.wait()


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
