"""M3c 验收：真 Claude agent 在真沙箱里回答，并在沙箱死后记得上一轮（真 LLM）。

默认 skip。要跑它需要三样东西同时到位：

    ROOST_CLAUDE_E2E=1
    ANTHROPIC_API_KEY=…                       # 只透传，不打印、不写盘
    docker build -t roost-claude examples/sandbox-images/claude/

（镜像名可用 `ROOST_CLAUDE_IMAGE` 覆盖。）

护的是附录 K 的验收面，而且只有真 LLM + 真沙箱能证明：

1. `ROOST_HARNESS` 选中的 Claude harness 在沙箱里真的把一个 turn 跑成了
   Delta + Terminal(ok)，凭据由 cold boot env 注入而来；
2. **记忆跟着工作区走**：turn 1 告诉 agent 一个只有它知道的口令 → `docker rm -f`
   掉沙箱 → turn 2 在一个新沙箱里问它那个口令，答案里必须有。会话 transcript
   与 session id 都落在工作区内，靠的是 M4 的快照/恢复，而不是任何进程内状态。

断言刻意只看"口令出现在文本里"这一件事：模型的措辞不可复现，把它当契约测会
变成一个天天误报的测试。
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from roost import (
    BackupCoordinator,
    DisplayEvent,
    DockerSandboxBackend,
    FileSnapshotStore,
    InProcessTurnDelivery,
    SandboxTurnRunner,
    SessionBootContext,
    SessionSandboxRegistry,
    SQLiteStateStore,
    TurnEnvelope,
    TurnProcessor,
)
from roost.backends import SANDBOX_LABEL

IMAGE = os.environ.get("ROOST_CLAUDE_IMAGE", "roost-claude")
BOOT_TIMEOUT = 240.0
STALL_TIMEOUT = 300.0
PASSPHRASE = "purple-otter-1917"


def _docker_available() -> bool:
    try:
        done = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _image_available() -> bool:
    try:
        done = subprocess.run(
            ["docker", "image", "inspect", IMAGE], capture_output=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


pytestmark = [
    pytest.mark.skipif(
        os.environ.get("ROOST_CLAUDE_E2E") != "1",
        reason="真 LLM e2e 需要 ROOST_CLAUDE_E2E=1",
    ),
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"), reason="缺 ANTHROPIC_API_KEY"
    ),
    pytest.mark.skipif(not _docker_available(), reason="docker daemon unavailable"),
    pytest.mark.skipif(
        not _image_available(),
        reason=f"缺镜像 {IMAGE}（docker build -t {IMAGE} examples/sandbox-images/claude/）",
    ),
]


def snapshot_key(session_id: str) -> str:
    return f"workspace/{session_id}.tar.gz"


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[DisplayEvent] = []

    async def emit(self, events: list[DisplayEvent]) -> None:
        self.events.extend(events)

    def text(self) -> str:
        return "".join(
            event.body["text"] for event in self.events if event.kind == "text"
        )

    def terminals(self) -> list[DisplayEvent]:
        return [event for event in self.events if event.kind == "terminal"]


class ClaudeBootContext:
    """SessionContextProvider：选中 Claude harness，并把凭据交给库注入沙箱。"""

    async def cold_boot_context(self, session_id: str) -> SessionBootContext:
        del session_id
        env = {"ROOST_HARNESS": "roost.harness_claude:ClaudeHarness"}
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
            value = os.environ.get(name)
            if value:
                env[name] = value
        return SessionBootContext(env=env)


@pytest.fixture
def sandbox_cleanup():
    def labelled() -> set[str]:
        done = subprocess.run(
            ["docker", "ps", "-aq", "--no-trunc", "--filter", f"label={SANDBOX_LABEL}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return {line.strip() for line in done.stdout.splitlines() if line.strip()}

    before = labelled()
    try:
        yield
    finally:
        for sandbox_id in labelled() - before:
            subprocess.run(
                ["docker", "rm", "-f", sandbox_id], capture_output=True, timeout=120
            )


class Host:
    def __init__(self, snapshot_dir: Path) -> None:
        self.backend = DockerSandboxBackend(image=IMAGE)
        self.store = SQLiteStateStore(None)
        self.snapshots = FileSnapshotStore(snapshot_dir)
        self.sink = RecordingSink()
        self.backup = BackupCoordinator(self.snapshots, snapshot_key)
        self.registry = SessionSandboxRegistry(
            self.backend,
            self.store,
            sink=self.sink,
            snapshot_store=self.snapshots,
            snapshot_key=snapshot_key,
            template=IMAGE,
            context_provider=ClaudeBootContext(),
            boot_timeout=BOOT_TIMEOUT,
        )
        self.runner = SandboxTurnRunner(
            self.registry, self.sink, backup=self.backup, stall_timeout=STALL_TIMEOUT
        )
        self.delivery = InProcessTurnDelivery()
        self.processor = TurnProcessor(
            self.store, self.runner, delivery=self.delivery, lock_seconds=600
        )
        self.delivery.start(self.processor.process)

    async def say(self, session_id: str, turn_id: str, text: str) -> None:
        await self.delivery.enqueue(
            TurnEnvelope(turn_id=turn_id, session_id=session_id, payload={"text": text})
        )
        await self.delivery.join()
        await self.backup.drain()

    async def close(self) -> None:
        await self.delivery.stop()
        await self.backup.drain()
        await self.store.close()


@pytest.fixture
async def host(sandbox_cleanup, tmp_path: Path):
    instance = Host(tmp_path / "snapshots")
    try:
        yield instance
    finally:
        await instance.close()


async def test_claude_answers_and_remembers_across_sandbox_death(host) -> None:
    session = f"claude-{uuid.uuid4().hex[:8]}"

    await host.say(
        session,
        "turn-1",
        f"Remember this passphrase exactly: {PASSPHRASE}. "
        "Reply with just the word ACK, nothing else.",
    )
    first_terminal = host.sink.terminals()[-1]
    assert first_terminal.body["status"] == "ok", first_terminal.body
    assert host.sink.text().strip(), "第一个 turn 没有任何文本输出"

    first = await host.store.get_binding(session)
    assert first is not None
    subprocess.run(
        ["docker", "rm", "-f", first.sandbox_id], capture_output=True, timeout=120
    )

    before = len(host.sink.events)
    await host.say(
        session,
        "turn-2",
        "What was the passphrase I gave you earlier? Reply with just the passphrase.",
    )

    second = await host.store.get_binding(session)
    assert second is not None and second.sandbox_id != first.sandbox_id
    answer = "".join(
        event.body["text"] for event in host.sink.events[before:] if event.kind == "text"
    )
    # 记忆确实跟着工作区回来了——新沙箱、新进程、只有恢复出来的 transcript。
    assert PASSPHRASE in answer, answer
    assert host.sink.terminals()[-1].body["status"] == "ok"
