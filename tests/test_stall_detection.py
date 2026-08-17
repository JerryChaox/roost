"""runner 的停滞判定：颗粒无收才算卡死，慢而活着不算（CONTRACTS.md 附录 H / I3）。

防的回归——这两条恰好是同一个判定的两个方向，任何一边错了都很贵：

- 漏杀：事件流真的停了却一直等下去，锁被心跳续着，sweep 永远看不到这一行，
  用户的消息永久没有答复（M5 存在的理由）。
- 误杀：慢而活着的 harness（事件之间隔得久但一直在出事件）被当成卡死，
  沙箱被销毁、turn 被重跑——一次正常的长任务被腰斩，还白烧一次 cold boot。

判定只看"含事件的响应页"：空长轮询页不重置计时。这里的假 client 因此刻意用
"空页 + 有事件页"两种返回来把两个方向都钉住，不依赖真 docker。
"""

from __future__ import annotations

import asyncio

import pytest

from roost import DisplayEvent, SandboxHandle, TurnEnvelope
from roost.control.client import EventPage, TurnSubmission
from roost.events import Delta, DriverEvent, Terminal
from roost.runner import SandboxTurnRunner, TurnStalledError

HANDLE = SandboxHandle(sandbox_id="sbx-stall", backend="fake")
STALL = 0.3


def make_turn(turn_id: str = "turn-1") -> TurnEnvelope:
    return TurnEnvelope(turn_id=turn_id, session_id="session-1", payload={"text": "hi"})


class FakeRegistry:
    """只提供 runner 用得到的两个动作：取沙箱、销毁沙箱。"""

    def __init__(self, client: "ScriptedClient") -> None:
        self._client = client
        self.destroyed: list[SandboxHandle] = []

    async def get_or_create(self, session_id: str, *, turn_id: str = ""):
        return HANDLE, self._client

    async def destroy(self, handle: SandboxHandle) -> None:
        self.destroyed.append(handle)


class ScriptedClient:
    """按脚本返回事件页的假 ControlClient。

    脚本里的每一项是一批事件；空批表示"一次没等到东西的长轮询"。脚本耗尽后
    永远返回空页——这正是 driver 卡死时宿主看到的样子。
    """

    def __init__(self, script: list[list[DriverEvent]], *, page_delay: float = 0.05) -> None:
        self._script = list(script)
        self._page_delay = page_delay
        self.wait_ms_seen: list[int] = []
        self.fetches = 0

    async def submit_turn(self, turn: TurnEnvelope) -> TurnSubmission:
        return TurnSubmission(turn_id=turn.turn_id, state="accepted", turn_state="running")

    async def fetch_events(self, turn_id: str, *, after: int = 0, wait_ms: int) -> EventPage:
        self.fetches += 1
        self.wait_ms_seen.append(wait_ms)
        await asyncio.sleep(min(self._page_delay, wait_ms / 1000))
        events = self._script.pop(0) if self._script else []
        for offset, event in enumerate(events, start=1):
            object.__setattr__(event, "seq", after + offset)
        return EventPage(events=events, next_after=after + len(events))


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[DisplayEvent] = []

    async def emit(self, events: list[DisplayEvent]) -> None:
        self.events.extend(events)


class RecordingOps:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type: str, /, **details: object) -> None:
        self.events.append((event_type, dict(details)))

    def kinds(self) -> list[str]:
        return [event for event, _ in self.events]

    def details(self, event_type: str) -> dict:
        for name, details in self.events:
            if name == event_type:
                return details
        raise AssertionError(f"没有记录 {event_type!r}：{self.kinds()}")


class RecordingBackup:
    def __init__(self) -> None:
        self.scheduled: list[str] = []

    def schedule(self, session_id: str, client) -> None:
        self.scheduled.append(session_id)


def delta(turn_id: str, text: str) -> Delta:
    return Delta(turn_id=turn_id, text=text, seq=0)


def terminal(turn_id: str) -> Terminal:
    return Terminal(turn_id=turn_id, status="ok", error=None, usage={}, seq=0)


async def test_silent_stream_kills_the_sandbox_and_raises() -> None:
    """事件流颗粒无收超过 stall_timeout：销毁沙箱 + TurnStalledError。"""
    client = ScriptedClient([])            # 一个事件都不给：卡死的 driver
    registry = FakeRegistry(client)
    ops = RecordingOps()
    backup = RecordingBackup()
    runner = SandboxTurnRunner(
        registry, RecordingSink(), backup=backup, ops=ops, wait_ms=1_000, stall_timeout=STALL
    )
    turn = make_turn()

    with pytest.raises(TurnStalledError) as excinfo:
        await runner.run(turn)

    assert excinfo.value.turn_id == turn.turn_id
    assert registry.destroyed == [HANDLE]           # 沙箱确实被销毁了
    assert ops.details("sandbox_stalled_killed") == {
        "turn_id": turn.turn_id,
        "session_id": turn.session_id,
        "sandbox_id": HANDLE.sandbox_id,
        "stall_timeout": STALL,
    }
    # 不向一个已经不响应的 driver 要工作区：停滞路径不备份。
    assert backup.scheduled == []


async def test_stall_deadline_is_not_stretched_by_a_long_wait_ms() -> None:
    """每页等待取 min(wait_ms, 剩余预算)：判定粒度归 stall_timeout，不被 wait_ms 拖长。"""
    client = ScriptedClient([], page_delay=0.02)
    registry = FakeRegistry(client)
    runner = SandboxTurnRunner(
        registry, RecordingSink(), wait_ms=30_000, stall_timeout=STALL
    )

    with pytest.raises(TurnStalledError):
        await runner.run(make_turn())

    assert client.wait_ms_seen, "至少要拉过一次事件"
    assert max(client.wait_ms_seen) <= int(STALL * 1000)
    # 预算随时间收窄，最后一页的等待不会比第一页还长。
    assert client.wait_ms_seen[-1] <= client.wait_ms_seen[0]


async def test_slow_but_alive_stream_is_never_killed() -> None:
    """事件间隔逼近但不超过 stall_timeout 的慢 turn：正常完成，沙箱不动。"""
    turn = make_turn()
    # 每两个事件页之间夹一个空页，间隔 ~0.2s < stall_timeout 0.3s。
    script: list[list[DriverEvent]] = []
    for index in range(4):
        script.append([])
        script.append([delta(turn.turn_id, f"chunk{index}")])
    script.append([])
    script.append([terminal(turn.turn_id)])

    client = ScriptedClient(script, page_delay=0.1)
    registry = FakeRegistry(client)
    sink = RecordingSink()
    backup = RecordingBackup()
    runner = SandboxTurnRunner(
        registry, sink, backup=backup, wait_ms=1_000, stall_timeout=STALL
    )

    await runner.run(turn)   # 不抛

    assert registry.destroyed == []
    assert backup.scheduled == [turn.session_id]
    text = "".join(
        event.body["text"] for event in sink.events if event.kind == "text"
    )
    assert text == "chunk0chunk1chunk2chunk3"


async def test_stall_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError):
        SandboxTurnRunner(FakeRegistry(ScriptedClient([])), RecordingSink(), stall_timeout=0)
