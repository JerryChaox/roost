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

它同时是 **Demo 2（持久化）** 的人工入口：`--counter` 让每条消息去递增沙箱工作区里
的计数器，`/kill` 当场 `docker rm -f` 掉沙箱；下一条消息会 cold boot 一个新沙箱、
把上一次的工作区快照灌回去，计数器因此**续增**而不是从 1 重来。

    .venv/bin/python examples/cli_chat.py --counter --snapshot-dir /tmp/roost-snap

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
    BackupCoordinator,
    KIND_TERMINAL,
    KIND_TEXT,
    KIND_TOOL,
    DisplayEvent,
    DockerSandboxBackend,
    FileSnapshotStore,
    InProcessTurnDelivery,
    SandboxTurnRunner,
    SessionSandboxRegistry,
    SQLiteStateStore,
    TurnEnvelope,
    TurnProcessor,
)

DEFAULT_SESSION = "cli-demo"
DEFAULT_IMAGE = "python:3.12-slim"
DEFAULT_SNAPSHOT_DIR = ".roost-snapshots"


def snapshot_key(session_id: str) -> str:
    """SnapshotKeyFn：session → 存储 key。**宿主的决定**——库对 key 结构不作解释。"""
    return f"workspace/{session_id}.tar.gz"


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
    snapshots = FileSnapshotStore(args.snapshot_dir)
    backup = BackupCoordinator(snapshots, snapshot_key)
    registry = SessionSandboxRegistry(
        backend,
        store,
        sink=sink,
        snapshot_store=snapshots,
        snapshot_key=snapshot_key,
        template=args.image,
        boot_timeout=args.boot_timeout,
    )
    runner = SandboxTurnRunner(registry, sink, backup=backup)
    delivery = InProcessTurnDelivery(duplicate_factor=2 if args.duplicate else 1)
    processor = TurnProcessor(store, runner, delivery=delivery)
    delivery.start(processor.process)

    print(
        f"roost demo — session={args.session} backend=docker image={args.image}"
        f" snapshots={args.snapshot_dir}"
        + ("  [每条消息双投]" if args.duplicate else "")
        + ("  [counter]" if args.counter else ""),
        flush=True,
    )

    try:
        for index, line in enumerate(_lines(args.message)):
            text = line.strip()
            if not text:
                continue
            if text in {"/quit", "/exit"}:
                break
            if text == "/kill":
                await _kill_sandbox(backend, store, args.session)
                continue
            turn = TurnEnvelope(
                turn_id=derive_turn_id(args.session, index, text),
                session_id=args.session,
                payload={"text": text, "counter": True} if args.counter else {"text": text},
            )
            if args.message:            # 非交互模式没有 input() 的回显，自己打一行
                print(f"you> {text}", flush=True)
            await delivery.enqueue(turn)
            await delivery.join()
            # 备份是 fire-and-forget 的（turn 从不等它）；demo 在这里等一下，是为了
            # 让紧接着的 /kill 有一份确定已经写好的快照可恢复。真实宿主不该这么做。
            await backup.drain()
    finally:
        await delivery.stop()
        await backup.drain()
        await _cleanup(backend, store, args)
    return 0


async def _kill_sandbox(
    backend: DockerSandboxBackend, store: SQLiteStateStore, session_id: str
) -> None:
    """`/kill`：当场销毁当前沙箱（Demo 2 的"沙箱是一次性的"那一半）。

    刻意**不动绑定行**：绑定指向一个已经不存在的容器，正是下一个 turn 要处理的
    现实——registry 的 health 探测会发现它死了，然后 cold boot + 恢复快照。
    """
    binding = await store.get_binding(session_id)
    if binding is None:
        print("[kill] 当前没有绑定的沙箱", flush=True)
        return
    print(f"[kill] docker rm -f {binding.sandbox_id[:12]}", flush=True)
    try:
        await backend.kill(binding)
    except Exception as exc:
        print(f"[kill] 失败：{exc!r}", flush=True)


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
        "--counter", action="store_true",
        help="每条消息递增工作区计数器（Demo 2：配合 /kill 看它跨沙箱续增）",
    )
    parser.add_argument(
        "--snapshot-dir", default=DEFAULT_SNAPSHOT_DIR, help="工作区快照目录"
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
