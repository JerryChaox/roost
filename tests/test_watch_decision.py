"""双时钟与决策矩阵（CONTRACTS.md 附录 M）——runner 判活的全部分支。

防的回归，两个方向都很贵，而且是同一个判定的两面：

- **漏杀**：事件流真停了却一直等下去，锁被心跳续着，sweep 永远看不到这一行，
  用户的消息永久没有答复。
- **误杀**：慢而活着的 turn（一次 90 秒的 API 调用、一个长 tool、一段
  extended thinking）被当成卡死，沙箱被销毁、turn 被重跑——一次正常的长任务被
  腰斩，还白烧一次 cold boot。

M5 用单一 stall clock 判这件事，两边都会错。M12 的判据是两个时钟加一次内核层面的
活性探测，五行矩阵各有一条用例。矩阵之外还有三件事在这里钉住：升级阶梯的三级、
wall-clock ceiling 落 attention 而不是 failed、以及**普通轮次零写入**的观测纪律。

时间用真实的亚秒阈值（不 patch time.monotonic：那会连 asyncio 的定时器一起改掉，
测试就不再测被测对象了）；liveness 则完全由假 client 的响应字段驱动，与时间无关。
"""

from __future__ import annotations

import asyncio

import pytest

from roost import DisplayEvent, SandboxHandle, TurnEnvelope
from roost.control.client import EventPage, TurnSubmission
from roost.driver.probe import ProbeResult
from roost.events import Delta, DriverEvent, Terminal
from roost.runner import (
    SandboxTurnRunner,
    TurnAbandonedError,
    TurnNeedsAttentionError,
    TurnStalledError,
)

HANDLE = SandboxHandle(sandbox_id="sbx-watch", backend="fake")

# 亚秒阈值：判定的语义与量纲无关，测试因此可以跑得快。
QUICK = 0.2
GENEROUS = 30.0


def make_turn(turn_id: str = "turn-1") -> TurnEnvelope:
    return TurnEnvelope(turn_id=turn_id, session_id="session-1", payload={"text": "hi"})


def delta(turn_id: str, text: str = "chunk") -> Delta:
    return Delta(turn_id=turn_id, text=text, seq=0)


def terminal(turn_id: str) -> Terminal:
    return Terminal(turn_id=turn_id, status="ok", error=None, usage={}, seq=0)


def active(reason: str = "pid 7 (node) state=R") -> ProbeResult:
    return ProbeResult(active=True, reason=reason, driver_pid=7, processes=[])


def idle(reason: str = "no_active_process") -> ProbeResult:
    return ProbeResult(active=False, reason=reason, driver_pid=7, processes=[])


class ScriptedClient:
    """按脚本返回事件页的假 ControlClient。

    脚本里每一项是一批事件；空批 = 一次没等到东西的长轮询。脚本耗尽后永远返回
    空页——那正是 driver 卡死时宿主看到的样子。`liveness_quiet_ms` 由构造参数
    直接给定（可调），因此 liveness 与 progress 两个时钟在测试里可以独立摆布：
    真实系统里它们相关，判定的正确性却必须对每一种组合都成立。
    """

    def __init__(
        self,
        script: list[list[DriverEvent]],
        *,
        page_delay: float = 0.05,
        liveness_quiet_ms: int | None = 0,
    ) -> None:
        self._script = list(script)
        self._page_delay = page_delay
        self.liveness_quiet_ms = liveness_quiet_ms
        self.submits = 0
        self.fetches = 0
        self.wait_ms_seen: list[int] = []

    async def submit_turn(self, turn: TurnEnvelope) -> TurnSubmission:
        self.submits += 1
        return TurnSubmission(
            turn_id=turn.turn_id, state="accepted", turn_state="running"
        )

    async def fetch_events(
        self, turn_id: str, *, after: int = 0, wait_ms: int
    ) -> EventPage:
        self.fetches += 1
        self.wait_ms_seen.append(wait_ms)
        await asyncio.sleep(min(self._page_delay, max(wait_ms, 0) / 1000))
        events = self._script.pop(0) if self._script else []
        for offset, event in enumerate(events, start=1):
            object.__setattr__(event, "seq", after + offset)
        return EventPage(
            events=events,
            next_after=after + len(events),
            liveness_quiet_ms=self.liveness_quiet_ms,
        )


class FakeRegistry:
    """runner 用得到的四个动作：取沙箱、销毁、探测活性、重启 driver。"""

    def __init__(
        self,
        client: ScriptedClient,
        *,
        probe: ProbeResult | None = None,
        restart_to: ScriptedClient | None = None,
        boot_delay: float = 0.0,
        restart_error: Exception | None = None,
    ) -> None:
        self._client = client
        self._probe = probe if probe is not None else idle()
        self._restart_to = restart_to
        self._boot_delay = boot_delay
        self._restart_error = restart_error
        self.destroyed: list[SandboxHandle] = []
        self.probes = 0
        self.restarts = 0

    async def get_or_create(self, session_id: str, *, turn_id: str = ""):
        if self._boot_delay:
            await asyncio.sleep(self._boot_delay)
        return HANDLE, self._client

    async def destroy(self, handle: SandboxHandle) -> None:
        self.destroyed.append(handle)

    async def probe_activity(self, handle: SandboxHandle) -> ProbeResult:
        self.probes += 1
        return self._probe

    async def restart_driver(self, session_id: str, handle: SandboxHandle):
        self.restarts += 1
        if self._restart_error is not None:
            raise self._restart_error
        return self._restart_to if self._restart_to is not None else self._client


class FakeStore:
    """只提供 runner 用到的两个方法：阶梯计数与终态写入。"""

    def __init__(self, *, ordinal: int = 1) -> None:
        self._ordinal = ordinal
        self.bumps = 0
        self.finished: list[tuple[str, str]] = []

    async def bump_error_ordinal(self, turn_id: str) -> int:
        self.bumps += 1
        return self._ordinal

    async def finish_turn(self, turn_id: str, *, status: str) -> None:
        self.finished.append((turn_id, status))


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[DisplayEvent] = []

    async def emit(self, events: list[DisplayEvent]) -> None:
        self.events.extend(events)

    def text(self) -> str:
        return "".join(e.body["text"] for e in self.events if e.kind == "text")


class RecordingOps:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event_type: str, /, **details: object) -> None:
        self.events.append((event_type, dict(details)))

    def kinds(self) -> list[str]:
        return [name for name, _ in self.events]

    def all_of(self, event_type: str) -> list[dict]:
        return [details for name, details in self.events if name == event_type]

    def details(self, event_type: str) -> dict:
        found = self.all_of(event_type)
        if not found:
            raise AssertionError(f"没有记录 {event_type!r}：{self.kinds()}")
        return found[0]


def build(
    registry: FakeRegistry,
    *,
    store: FakeStore | None = None,
    ops: RecordingOps | None = None,
    sink: RecordingSink | None = None,
    **thresholds,
) -> SandboxTurnRunner:
    """默认全部阈值调到"不会碰到"，每个用例只放开它要考的那一个。"""
    defaults = dict(
        wait_ms=200,
        boot_grace=0.01,
        liveness_quiet=GENEROUS,
        progress_quiet=GENEROUS,
        first_renderable=GENEROUS,
        round_budget=10_000,
        long_watch_interval=GENEROUS,
        wall_clock_ceiling=GENEROUS,
    )
    defaults.update(thresholds)
    return SandboxTurnRunner(
        registry,
        sink if sink is not None else RecordingSink(),
        store=store,
        ops=ops,
        **defaults,
    )


# ---- 决策矩阵五行 ---------------------------------------------------------


async def test_row1_fresh_liveness_and_progress_keeps_watching() -> None:
    """第一行：两个时钟都新鲜 → 继续 watch，一路跑到 Terminal。"""
    turn = make_turn()
    script: list[list[DriverEvent]] = []
    for index in range(3):
        script.append([])                                  # 空长轮询页
        script.append([delta(turn.turn_id, f"c{index}")])
    script.append([terminal(turn.turn_id)])

    client = ScriptedClient(script, page_delay=0.05, liveness_quiet_ms=0)
    registry = FakeRegistry(client)
    sink = RecordingSink()
    ops = RecordingOps()

    await build(registry, ops=ops, sink=sink, progress_quiet=QUICK).run(turn)

    assert sink.text() == "c0c1c2"
    assert registry.destroyed == []
    assert registry.probes == 0            # 没到判定，就不该去打扰内核
    assert ops.kinds() == ["turn_submitted"]


async def test_row2_fresh_liveness_but_exhausted_progress_is_a_real_hang() -> None:
    """第二行：liveness 新鲜、progress 竭 → 杀，**即使 probe 说 ACTIVE**。

    这是"心跳还在发、可渲染事件一个也没有"的真 hang：进程忙不代表 turn 在推进。
    """
    turn = make_turn()
    client = ScriptedClient([], page_delay=0.03, liveness_quiet_ms=0)
    registry = FakeRegistry(client, probe=active())
    ops = RecordingOps()
    store = FakeStore(ordinal=2)

    with pytest.raises(TurnStalledError):
        await build(
            registry, store=store, ops=ops, first_renderable=QUICK
        ).run(turn)

    killed = ops.details("sandbox_stalled_killed")
    assert killed["threshold_tripped"] == "progress"
    assert killed["liveness_quiet_ms"] == 0
    # 探测仍然跑了一次，但只作为诊断快照进 ops——它没有资格改变这一行的结论。
    assert registry.probes == 1
    assert killed["probe_active"] is True
    assert registry.destroyed == [HANDLE]


async def test_row3_quiet_but_active_defers_and_keeps_watching() -> None:
    """第三行：liveness 竭、progress 未竭、probe ACTIVE → silence-defer，继续。"""
    turn = make_turn()
    # 先若干空页（触发 defer），再给出答案。
    script: list[list[DriverEvent]] = [[], [], [], [delta(turn.turn_id, "slow")],
                                       [terminal(turn.turn_id)]]
    client = ScriptedClient(script, page_delay=0.03, liveness_quiet_ms=99_000)
    registry = FakeRegistry(client, probe=active())
    ops = RecordingOps()
    sink = RecordingSink()

    await build(
        registry, ops=ops, sink=sink, liveness_quiet=QUICK, progress_quiet=GENEROUS
    ).run(turn)

    assert sink.text() == "slow"
    assert registry.destroyed == []
    assert registry.probes >= 3            # 每一轮安静都重新问一次内核
    deferred = ops.all_of("watch_silence_deferred")
    assert len(deferred) == 1              # 计数无上限，写入只有第一次
    assert deferred[0]["silence_deferred_count"] == 1
    assert deferred[0]["liveness_quiet_ms"] == 99_000
    assert deferred[0]["probe_active"] is True


async def test_row4_quiet_and_not_active_is_killed() -> None:
    """第四行：liveness 竭、progress 未竭、probe 非 ACTIVE → 杀，threshold=liveness。"""
    turn = make_turn()
    client = ScriptedClient([], page_delay=0.03, liveness_quiet_ms=99_000)
    registry = FakeRegistry(client, probe=idle())
    ops = RecordingOps()

    with pytest.raises(TurnStalledError) as excinfo:
        await build(
            registry, store=FakeStore(ordinal=2), ops=ops, liveness_quiet=QUICK
        ).run(turn)

    assert excinfo.value.threshold_tripped == "liveness"
    assert ops.details("sandbox_stalled_killed")["threshold_tripped"] == "liveness"
    assert ops.all_of("watch_silence_deferred") == []
    assert registry.destroyed == [HANDLE]


async def test_row5_both_clocks_exhausted_trips_on_liveness() -> None:
    """第五行：两个时钟都竭 → 杀，threshold_tripped=liveness（矩阵逐字如此）。"""
    turn = make_turn()
    client = ScriptedClient([], page_delay=0.03, liveness_quiet_ms=99_000)
    registry = FakeRegistry(client, probe=active())      # 探测说什么都不影响这一行
    ops = RecordingOps()

    with pytest.raises(TurnStalledError):
        await build(
            registry,
            store=FakeStore(ordinal=2),
            ops=ops,
            liveness_quiet=QUICK,
            first_renderable=QUICK,
        ).run(turn)

    assert ops.details("sandbox_stalled_killed")["threshold_tripped"] == "liveness"


# ---- 时钟的两条边界 -------------------------------------------------------


async def test_boot_grace_exempts_the_silent_period_before_the_first_event() -> None:
    """首个事件之前整段豁免：cold boot 的合法静默不该触发 reload 风暴。"""
    turn = make_turn()
    # 前 0.3s 一片空白（超过 first_renderable 0.05），随后才出事件。
    script: list[list[DriverEvent]] = [
        [], [], [], [], [], [], [delta(turn.turn_id, "late")], [terminal(turn.turn_id)]
    ]
    client = ScriptedClient(script, page_delay=0.05, liveness_quiet_ms=0)
    registry = FakeRegistry(client)
    sink = RecordingSink()

    await build(
        registry, sink=sink, boot_grace=5.0, first_renderable=0.05
    ).run(turn)

    assert sink.text() == "late"
    assert registry.destroyed == []


async def test_progress_clock_starts_at_submit_not_before() -> None:
    """progress 时间戳 clamp 到提交时刻：上一段 idle gap 不算进这个 turn。

    boot（这里用 get_or_create 的耗时表示）花掉的时间比 first_renderable 还长，
    但事件在提交之后很快就来了——不 clamp 的判定会在首轮当场误杀。
    """
    turn = make_turn()
    client = ScriptedClient(
        [[delta(turn.turn_id, "prompt")], [terminal(turn.turn_id)]],
        page_delay=0.02,
        liveness_quiet_ms=0,
    )
    registry = FakeRegistry(client, boot_delay=0.3)
    sink = RecordingSink()

    await build(registry, sink=sink, boot_grace=0.01, first_renderable=0.15).run(turn)

    assert sink.text() == "prompt"
    assert registry.destroyed == []


async def test_missing_liveness_field_falls_back_to_progress_only() -> None:
    """老 driver 不给 liveness_quiet_ms：按"不支持"处理，只用 progress 时钟。

    把缺失当 0 会让 liveness 永远新鲜（无害但退化），当 ∞ 则会让每个老沙箱一上来
    就被判死——后者是真正会出事的读法，这条用例钉的是它。
    """
    turn = make_turn()
    script: list[list[DriverEvent]] = [
        [], [delta(turn.turn_id, "old")], [], [terminal(turn.turn_id)]
    ]
    client = ScriptedClient(script, page_delay=0.05, liveness_quiet_ms=None)
    registry = FakeRegistry(client, probe=idle())
    sink = RecordingSink()

    await build(registry, sink=sink, liveness_quiet=0.01, progress_quiet=GENEROUS).run(turn)

    assert sink.text() == "old"
    assert registry.destroyed == []
    assert registry.probes == 0        # liveness 不参与判定，就没有 probe 的理由


# ---- 升级阶梯三级 ---------------------------------------------------------


async def test_ladder_first_rung_restarts_the_driver_and_resubmits() -> None:
    """ordinal 1：重启 driver 进程 → 重新提交 → 新进程给出答案，沙箱不动。"""
    turn = make_turn()
    dead = ScriptedClient([], page_delay=0.03, liveness_quiet_ms=99_000)
    revived = ScriptedClient(
        [[delta(turn.turn_id, "after-restart")], [terminal(turn.turn_id)]],
        page_delay=0.02,
        liveness_quiet_ms=0,
    )
    registry = FakeRegistry(dead, probe=idle(), restart_to=revived)
    store = FakeStore(ordinal=1)
    ops = RecordingOps()
    sink = RecordingSink()

    await build(
        registry, store=store, ops=ops, sink=sink, liveness_quiet=QUICK
    ).run(turn)

    assert registry.restarts == 1
    assert registry.destroyed == []                 # 第一级不碰沙箱
    assert sink.text() == "after-restart"
    assert revived.submits == 1                     # 空 registry 的新进程重投合法
    assert store.finished == []
    restart = ops.details("driver_restart_requested")
    assert restart["error_ordinal"] == 1
    assert [name for name, _ in ops.events].count("turn_submitted") == 2


async def test_ladder_second_rung_kills_the_sandbox() -> None:
    """ordinal 2：杀沙箱 + TurnStalledError（既有的 sweep → requeue 路径）。"""
    turn = make_turn()
    client = ScriptedClient([], page_delay=0.03, liveness_quiet_ms=99_000)
    registry = FakeRegistry(client, probe=idle())
    store = FakeStore(ordinal=2)

    with pytest.raises(TurnStalledError):
        await build(registry, store=store, liveness_quiet=QUICK).run(turn)

    assert registry.restarts == 0
    assert registry.destroyed == [HANDLE]
    assert store.finished == []        # 不收尾：锁自然过期，交给 sweep


async def test_ladder_last_rung_abandons_the_turn() -> None:
    """ordinal 6：finish_turn('failed') + ops 终止事件，不再重投。"""
    turn = make_turn()
    client = ScriptedClient([], page_delay=0.03, liveness_quiet_ms=99_000)
    registry = FakeRegistry(client, probe=idle())
    store = FakeStore(ordinal=6)
    ops = RecordingOps()

    with pytest.raises(TurnAbandonedError) as excinfo:
        await build(registry, store=store, ops=ops, liveness_quiet=QUICK).run(turn)

    assert excinfo.value.ordinal == 6
    assert store.finished == [(turn.turn_id, "failed")]
    assert registry.destroyed == [HANDLE]
    assert ops.details("turn_abandoned")["error_ordinal"] == 6


async def test_failed_restart_falls_through_to_killing_the_sandbox() -> None:
    """第一级失败不许原地打转：restart 起不来就当场升到杀沙箱。"""
    turn = make_turn()
    client = ScriptedClient([], page_delay=0.03, liveness_quiet_ms=99_000)
    registry = FakeRegistry(
        client, probe=idle(), restart_error=RuntimeError("boom")
    )
    ops = RecordingOps()

    with pytest.raises(TurnStalledError):
        await build(
            registry, store=FakeStore(ordinal=1), ops=ops, liveness_quiet=QUICK
        ).run(turn)

    assert registry.restarts == 1
    assert registry.destroyed == [HANDLE]
    assert ops.all_of("driver_restart_failed")


async def test_without_a_store_the_ladder_degrades_to_killing() -> None:
    """没有 store 时阶梯降级成"只杀沙箱"：绝不会 restart，也绝不会终止 turn。"""
    turn = make_turn()
    client = ScriptedClient([], page_delay=0.03, liveness_quiet_ms=99_000)
    registry = FakeRegistry(client, probe=idle())

    with pytest.raises(TurnStalledError):
        await build(registry, liveness_quiet=QUICK).run(turn)

    assert registry.restarts == 0
    assert registry.destroyed == [HANDLE]


# ---- 节奏、上限与观测纪律 --------------------------------------------------


async def test_round_budget_switches_to_long_watch_instead_of_ending_the_turn() -> None:
    """轮次超预算 → 放慢拉流，**不是**终止 turn（21 分钟的健康长 turn 曾被误终止）。"""
    turn = make_turn()
    script: list[list[DriverEvent]] = [[] for _ in range(4)]
    script.append([terminal(turn.turn_id)])
    client = ScriptedClient(script, page_delay=0.01, liveness_quiet_ms=0)
    registry = FakeRegistry(client)
    ops = RecordingOps()

    await build(
        registry, ops=ops, round_budget=2, long_watch_interval=1.0, wait_ms=50
    ).run(turn)

    assert registry.destroyed == []
    switches = ops.all_of("watch_long_watch_started")
    assert len(switches) == 1                       # 切换只写一次
    assert switches[0]["interval_ms"] == 1000
    assert max(client.wait_ms_seen) == 1000         # 之后的轮询确实放宽了
    assert client.wait_ms_seen[0] == 50


async def test_wall_clock_ceiling_records_attention_without_killing() -> None:
    """墙钟触顶：行落 'attention'，**沙箱保留**——现场留给人看，不是失败。"""
    turn = make_turn()
    # 一直有事件（两个时钟都健康），只是没完没了。
    client = ScriptedClient(
        [[delta(turn.turn_id, "x")] for _ in range(200)],
        page_delay=0.02,
        liveness_quiet_ms=0,
    )
    registry = FakeRegistry(client)
    store = FakeStore(ordinal=1)
    ops = RecordingOps()

    with pytest.raises(TurnNeedsAttentionError):
        await build(
            registry, store=store, ops=ops, wall_clock_ceiling=0.1
        ).run(turn)

    assert store.finished == [(turn.turn_id, "attention")]
    assert registry.destroyed == []                 # 关键：不杀沙箱
    assert store.bumps == 0                         # 也不动升级阶梯
    assert ops.details("turn_attention")["threshold_tripped"] == "wall_clock"


async def test_healthy_watch_is_write_free_except_for_the_start() -> None:
    """观测纪律：健康长 turn 全程只有一次写入（watch 开始）。

    每轮一写会让一个 6 小时的会话产出几千条无信息量的记录——真正出事时，
    有用的那几条就淹没在里面了。
    """
    turn = make_turn()
    script: list[list[DriverEvent]] = []
    for index in range(10):
        script.append([])
        script.append([delta(turn.turn_id, f"{index}")])
    script.append([terminal(turn.turn_id)])

    client = ScriptedClient(script, page_delay=0.01, liveness_quiet_ms=0)
    registry = FakeRegistry(client)
    ops = RecordingOps()

    await build(registry, ops=ops, wait_ms=20).run(turn)

    assert ops.kinds() == ["turn_submitted"]
    assert client.fetches > 20        # 轮次很多，写入仍然只有一条


async def test_threshold_arguments_must_be_positive() -> None:
    registry = FakeRegistry(ScriptedClient([]))
    for bad in (
        {"boot_grace": 0},
        {"liveness_quiet": 0},
        {"progress_quiet": -1},
        {"first_renderable": 0},
        {"wall_clock_ceiling": 0},
        {"round_budget": 0},
    ):
        with pytest.raises(ValueError):
            build(registry, **bad)
