#!/usr/bin/env python3
"""roost CLI demo 宿主 —— 一个最小的、真的把 turn 跑进沙箱的 REPL。

契约见 CONTRACTS.md《附录 F — 交付模块》。它同时是 **Demo 1（at-least-once 投递
下 exactly-once 执行）** 的人工入口：`--duplicate` 让每条消息投递两次，沙箱里的
agent 仍然只答一次。

    .venv/bin/python examples/cli_chat.py --duplicate
    .venv/bin/python examples/cli_chat.py --message "hello roost" --message bye

这是**宿主**代码，不是库代码：它自己决定 turn_id 怎么派生、事件怎么渲染、
沙箱用完要不要留——这三件事恰好是 roost 交还给宿主的部分。turn_id 取
`sha256(session_id + 行序号 + 文本)`，确定性派生意味着"同一条消息重投多少次都是
同一个 turn"，幂等因此可能成立。

M3b 的沙箱里跑的是 EchoHarness（回显 payload），所以 Demo 1 验的是投递与执行的
语义，与 LLM 无关；真实 Claude Agent SDK harness 归 M3c。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path

if __package__ in (None, ""):  # 直接 `python examples/cli_chat.py` 时也能跑
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from roost import (  # noqa: E402
    KIND_LIFECYCLE_NOTICE,
    KIND_TERMINAL,
    KIND_TEXT,
    KIND_TOOL,
    DisplayEvent,
    DockerSandboxBackend,
    InProcessTurnDelivery,
    SandboxTurnRunner,
    SessionSandboxRegistry,
    SQLiteStateStore,
    TurnEnvelope,
    TurnProcessor,
)

DEFAULT_SESSION = "cli-demo"
DEFAULT_IMAGE = "python:3.12-slim"


class ConsoleSink:
    """EventSink port 的终端实现：把 DisplayEvent 打成人看的行。"""

    def __init__(self) -> None:
        self._open_text = False

    async def emit(self, events: list[DisplayEvent]) -> None:
        for event in events:
            self._render(event)

    def _render(self, event: DisplayEvent) -> None:
        if event.kind == KIND_TEXT:
            if not self._open_text:
                print("agent> ", end="", flush=True)
                self._open_text = True
            print(event.body.get("text", ""), end="", flush=True)
            return

        self._close_text()
        if event.kind == KIND_LIFECYCLE_NOTICE:
            print(
                f"[lifecycle] {event.body.get('kind')} "
                f"({event.body.get('elapsed_ms')}ms)",
                flush=True,
            )
        elif event.kind == KIND_TOOL:
            print(
                f"[tool] {event.body.get('name')} {event.body.get('phase')}", flush=True
            )
        elif event.kind == KIND_TERMINAL:
            status = event.body.get("status")
            error = event.body.get("error")
            print(
                f"[terminal] {status}" + (f" — {error}" if error else "")
                + f"  usage={event.body.get('usage')}",
                flush=True,
            )

    def _close_text(self) -> None:
        if self._open_text:
            print(flush=True)
            self._open_text = False


def derive_turn_id(session_id: str, index: int, text: str) -> str:
    """确定性 turn_id：同一条消息无论投递几次都是同一个幂等主键。"""
    digest = hashlib.sha256(f"{session_id}\n{index}\n{text}".encode()).hexdigest()
    return f"turn-{digest[:32]}"


async def chat(args: argparse.Namespace) -> int:
    backend = DockerSandboxBackend(image=args.image)
    store = SQLiteStateStore(args.db)
    sink = ConsoleSink()
    registry = SessionSandboxRegistry(
        backend, store, sink=sink, template=args.image, boot_timeout=args.boot_timeout
    )
    runner = SandboxTurnRunner(registry, sink)
    delivery = InProcessTurnDelivery(duplicate_factor=2 if args.duplicate else 1)
    processor = TurnProcessor(store, runner, delivery=delivery)
    delivery.start(processor.process)

    print(
        f"roost demo — session={args.session} backend=docker image={args.image}"
        + ("  [每条消息双投]" if args.duplicate else ""),
        flush=True,
    )

    try:
        for index, line in enumerate(_lines(args.message)):
            text = line.strip()
            if not text:
                continue
            if text in {"/quit", "/exit"}:
                break
            turn = TurnEnvelope(
                turn_id=derive_turn_id(args.session, index, text),
                session_id=args.session,
                payload={"text": text},
            )
            if args.message:            # 非交互模式没有 input() 的回显，自己打一行
                print(f"you> {text}", flush=True)
            await delivery.enqueue(turn)
            await delivery.join()
    finally:
        await delivery.stop()
        await _cleanup(backend, store, args)
    return 0


def _lines(messages: list[str]):
    """非交互模式取 --message，否则从 stdin 逐行读。

    input() 会阻塞事件循环——对 REPL 无所谓（等输入时没有在飞的 turn），
    但这就是 demo 宿主与真实宿主的分界线，别照抄进服务端。
    """
    if messages:
        yield from messages
        return
    while True:
        try:
            line = input("you> ")
        except EOFError:
            return
        yield line


async def _cleanup(
    backend: DockerSandboxBackend, store: SQLiteStateStore, args: argparse.Namespace
) -> None:
    binding = await store.get_binding(args.session)
    if binding is not None and not args.keep_sandbox:
        print(f"[cleanup] docker rm -f {binding.sandbox_id[:12]}", flush=True)
        try:
            await backend.kill(binding)
        except Exception as exc:  # 清理失败不该盖掉 demo 的输出
            print(f"[cleanup] 失败：{exc!r}", flush=True)
    elif binding is not None:
        print(f"[cleanup] 保留沙箱 {binding.sandbox_id}", flush=True)
    await store.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="roost CLI chat demo")
    parser.add_argument("--session", default=DEFAULT_SESSION, help="session id")
    parser.add_argument(
        "--backend", default="docker", choices=["docker"], help="沙箱后端（M3b 仅 docker）"
    )
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="沙箱镜像")
    parser.add_argument("--db", default=None, help="SQLite 文件路径（默认内存库）")
    parser.add_argument(
        "--duplicate", action="store_true", help="每条消息投递两次（Demo 1）"
    )
    parser.add_argument(
        "--message", action="append", default=[], help="非交互模式的消息（可重复）"
    )
    parser.add_argument("--boot-timeout", type=float, default=60.0, help="cold boot 时限（秒）")
    parser.add_argument(
        "--keep-sandbox", action="store_true", help="退出时保留容器（排障用）"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(chat(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
