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

它同时是 **Demo 3（卡死恢复）** 的人工入口：短 `--stall-timeout/--lock-seconds`
配合消息里的 `hang_on_attempt`（EchoHarness 的挂起注入）能看到完整的一条恢复链——
沙箱被杀、turn 被 watchdog 重投、答案由新沙箱给出。

它同时是 **Demo 2（持久化）** 的人工入口：`--counter` 让每条消息去递增沙箱工作区里
的计数器，`/kill` 当场 `docker rm -f` 掉沙箱；下一条消息会 cold boot 一个新沙箱、
把上一次的工作区快照灌回去，计数器因此**续增**而不是从 1 重来。

    .venv/bin/python examples/cli_chat.py --counter --snapshot-dir /tmp/roost-snap

三个 demo 默认跑 EchoHarness（回显 payload），验的是投递与执行的语义，与 LLM 无关。
`--harness claude` 换成真实的 Claude Agent SDK harness（M3c）——它要求一个预装了
SDK 的镜像，并把本机的 ANTHROPIC_* 透传进沙箱 boot env：

    docker build -t roost-claude examples/sandbox-images/claude/
    .venv/bin/python examples/cli_chat.py --harness claude --image roost-claude
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path

if __package__ in (None, ""):  # 直接 `python examples/cli_chat.py` 时也能跑
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from roost import (  # noqa: E402
    KIND_LIFECYCLE_NOTICE,
    BackupCoordinator,
    DriverInstaller,
    KIND_TERMINAL,
    KIND_TEXT,
    KIND_TOOL,
    DisplayEvent,
    DockerSandboxBackend,
    SandboxBackend,
    FileSnapshotStore,
    InProcessTurnDelivery,
    SandboxTurnRunner,
    SessionBootContext,
    SessionSandboxRegistry,
    SQLiteStateStore,
    TurnEnvelope,
    TurnProcessor,
    Watchdog,
)

DEFAULT_SESSION = "cli-demo"
DEFAULT_IMAGE = "python:3.12-slim"
DEFAULT_SNAPSHOT_DIR = ".roost-snapshots"


def snapshot_key(session_id: str) -> str:
    """SnapshotKeyFn：session → 存储 key。**宿主的决定**——库对 key 结构不作解释。"""
    return f"workspace/{session_id}.tar.gz"


HARNESS_SPECS = {
    "echo": "roost.driver.harness:EchoHarness",
    "claude": "roost.harness_claude:ClaudeHarness",
}
#: 透传进沙箱的凭据环境变量（附录 K）。宿主只负责搬运，不解释、不打印。
ANTHROPIC_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")


class BootEnvProvider:
    """SessionContextProvider：cold boot 时把一份 env 交给库注入沙箱。

    这是 **demo 装配**，真实宿主会在这里去 secret store 现取。库对内容不作解释，
    也不会把它写进日志或快照——它只是 driver 启动命令前面的一串 `KEY=VALUE`。
    """

    def __init__(self, env: dict[str, str]) -> None:
        self._env = dict(env)

    async def cold_boot_context(self, session_id: str) -> SessionBootContext:
        del session_id                       # demo 的注入物与 session 无关
        return SessionBootContext(env=dict(self._env))


def boot_env(args: argparse.Namespace) -> dict[str, str]:
    """harness 选择 + 凭据 → 沙箱 boot env。

    `ROOST_HARNESS` 走 env 而不是镜像默认值：同一个镜像可以既跑 echo 又跑
    claude（既有测试与 demo 因此不受影响），而选择权留在宿主手上。
    """
    env = {"ROOST_HARNESS": HARNESS_SPECS[args.harness]}
    if args.harness == "claude":
        for name in ANTHROPIC_ENV_VARS:
            value = os.environ.get(name)
            if value:
                env[name] = value           # 值不打印，也不写进任何持久状态
    return env


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


def build_backend(args: argparse.Namespace):
    """`--backend` → (backend, template, installer)。

    两个 backend 的"模板"不是一回事：docker 版是镜像名，E2B 版是 template id
    （不给就是 E2B 默认 base 模板）。driver 的监听地址也不同——E2B 的端口代理
    可达沙箱内 loopback，所以那边把 driver 收到 127.0.0.1。
    """
    if args.backend == "e2b":
        from roost import E2BSandboxBackend  # 可选依赖：只在选了这个 backend 时才 import
        from roost.backends.e2b import DEFAULT_BIND_HOST

        backend = E2BSandboxBackend(
            template=args.template, sandbox_timeout=args.sandbox_timeout
        )
        return backend, args.template, DriverInstaller(bind_host=DEFAULT_BIND_HOST)
    return DockerSandboxBackend(image=args.image), args.image, None


async def chat(args: argparse.Namespace) -> int:
    if args.harness == "claude" and not (args.image_given or args.template):
        # 镜像归属是 M3c 的硬约束：SDK 与 Claude CLI 预装在镜像里，cold boot
        # 不装包。默认的 python:3.12-slim 里没有它们，跑起来只会在 driver
        # 启动时失败——不如在这里当场说清楚。
        print(
            "--harness claude 需要显式给出预装了 Claude Agent SDK 的镜像/模板："
            "\n  docker build -t roost-claude examples/sandbox-images/claude/"
            "\n  … --harness claude --image roost-claude",
            file=sys.stderr,
            flush=True,
        )
        return 2
    if args.harness == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("[warn] 本机没有 ANTHROPIC_API_KEY，沙箱里的 agent 会认证失败", flush=True)

    backend, template, installer = build_backend(args)
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
        template=template,
        installer=installer,
        context_provider=BootEnvProvider(boot_env(args)),
        boot_timeout=args.boot_timeout,
    )
    runner = SandboxTurnRunner(
        registry, sink, backup=backup, stall_timeout=args.stall_timeout
    )
    delivery = InProcessTurnDelivery(duplicate_factor=2 if args.duplicate else 1)
    processor = TurnProcessor(store, runner, delivery=delivery, lock_seconds=args.lock_seconds)
    delivery.start(processor.process)
    # Demo 3（卡死恢复）：沙箱里的 turn 挂死时，runner 杀沙箱、pipeline 放手，
    # 锁过期后由这个 watchdog 把 turn 重投给一个新沙箱。它是**唯一**的重投发起者。
    watchdog = Watchdog(store, delivery, interval=args.watchdog_interval)
    watchdog.start()

    print(
        f"roost demo — session={args.session} backend={args.backend}"
        f" harness={args.harness}"
        f" template={template or 'e2b-default'}"
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
            payload: dict = {"text": text}
            if args.counter:
                payload["counter"] = True
            if args.hang_first:
                # EchoHarness 的挂起注入：第一次尝试一个事件都不发（Demo 3）。
                payload["hang_on_attempt"] = 1
            turn = TurnEnvelope(
                turn_id=derive_turn_id(args.session, index, text),
                session_id=args.session,
                payload=payload,
            )
            if args.message:            # 非交互模式没有 input() 的回显，自己打一行
                print(f"you> {text}", flush=True)
            await delivery.enqueue(turn)
            await delivery.join()
            if args.hang_first:
                # 挂死的那一次结束时 turn 还没有答案：锁要先过期，watchdog 才看得见它。
                # 真实宿主不会这样等——它只管发消息，答案随事件流异步到达。
                print("[demo] 等待锁过期 → watchdog requeue → 新沙箱重跑…", flush=True)
                await asyncio.sleep(args.lock_seconds + args.watchdog_interval * 2)
                await delivery.join()
            # 备份是 fire-and-forget 的（turn 从不等它）；demo 在这里等一下，是为了
            # 让紧接着的 /kill 有一份确定已经写好的快照可恢复。真实宿主不该这么做。
            await backup.drain()
    finally:
        await watchdog.stop()
        await delivery.stop()
        await backup.drain()
        await _cleanup(backend, store, args)
    return 0


async def _kill_sandbox(
    backend: SandboxBackend, store: SQLiteStateStore, session_id: str
) -> None:
    """`/kill`：当场销毁当前沙箱（Demo 2 的"沙箱是一次性的"那一半）。

    刻意**不动绑定行**：绑定指向一个已经不存在的容器，正是下一个 turn 要处理的
    现实——registry 的 health 探测会发现它死了，然后 cold boot + 恢复快照。
    """
    binding = await store.get_binding(session_id)
    if binding is None:
        print("[kill] 当前没有绑定的沙箱", flush=True)
        return
    print(f"[kill] destroy sandbox {binding.sandbox_id[:12]}", flush=True)
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
    backend: SandboxBackend, store: SQLiteStateStore, args: argparse.Namespace
) -> None:
    binding = await store.get_binding(args.session)
    if binding is not None and not args.keep_sandbox:
        print(f"[cleanup] destroy sandbox {binding.sandbox_id[:12]}", flush=True)
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
        "--backend", default="docker", choices=["docker", "e2b"],
        help="沙箱后端（e2b 需要可选依赖 roost[e2b] 与 ROOST_E2B_API_KEY）",
    )
    parser.add_argument("--image", default=None, help="沙箱镜像（--backend docker）")
    parser.add_argument(
        "--harness", default="echo", choices=sorted(HARNESS_SPECS),
        help="沙箱里跑哪个 harness（claude 需要预装 SDK 的镜像，见 "
             "examples/sandbox-images/claude/）",
    )
    parser.add_argument(
        "--template", default=None,
        help="E2B template id（--backend e2b；不给则用 E2B 默认 base 模板）",
    )
    parser.add_argument(
        "--sandbox-timeout", type=int, default=None,
        help="E2B 沙箱存活时限（秒；不给用 SDK 默认 300s）",
    )
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
        "--stall-timeout", type=float, default=60.0,
        help="事件流停滞多久算沙箱卡死（秒；Demo 3 配合 payload 的 hang_on_attempt 用）",
    )
    parser.add_argument("--lock-seconds", type=int, default=30, help="turn 锁时长（秒）")
    parser.add_argument(
        "--hang-first", action="store_true",
        help="每条消息的第一次尝试在沙箱里挂死（Demo 3：看卡死→重投→新沙箱答复）",
    )
    parser.add_argument(
        "--watchdog-interval", type=float, default=5.0, help="watchdog sweep 间隔（秒）"
    )
    parser.add_argument(
        "--keep-sandbox", action="store_true", help="退出时保留容器（排障用）"
    )
    args = parser.parse_args(argv)
    # `--image` 的默认值要既能兜底又能被"有没有显式给"区分开：claude harness
    # 拒绝默认镜像（里面没有 SDK），echo harness 照旧用 python:3.12-slim。
    args.image_given = args.image is not None
    if args.image is None:
        args.image = DEFAULT_IMAGE
    return args


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(chat(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
