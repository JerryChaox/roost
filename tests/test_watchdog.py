"""Watchdog：sweep → attempt+1 重投这条唯一恢复路径的单元契约。

防的回归（CONTRACTS.md 附录 H / 附录 A）：

- 空 sweep 必须**完全无动作**：一个每 5 秒投一次空气的 watchdog 会把整条投递链
  变成噪声源，也会掩盖"真的有东西被重投了"这个信号。
- 非空 sweep 必须逐个 attempt+1 重投：attempt 的唯一 +1 所有者是投递方；
  这里少加一次，重投出去的信封就和挂死的那一次无法区分。
- stop 必须干净取消：后台任务泄漏会在测试之间互相污染，在宿主里则是关不掉的进程。
- **停滞的 turn 不得进入投递层的失败重投路径**：那会与 sweep 形成第二条恢复路径，
  两条路径各自 requeue 同一个 turn，就是 I1 明确排除的重复执行。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from conftest import FakeClock, make_turn

from roost import InProcessTurnDelivery, TurnEnvelope, TurnProcessor, Watchdog
from roost.runner import TurnStalledError

LOCK = 2


class FakeDelivery:
    """记录 enqueue 的最小 TurnDelivery。"""

    def __init__(self) -> None:
        self.enqueued: list[TurnEnvelope] = []
        self.fail_on: set[str] = set()

    async def enqueue(
        self, turn: TurnEnvelope, *, not_before: datetime | None = None
    ) -> None:
        if turn.turn_id in self.fail_on:
            raise RuntimeError("投递挂了")
        self.enqueued.append(turn)


class FakeStore:
    """只实现 sweep_due_turns 的最小 StateStore（watchdog 只用得到这一个方法）。"""

    def __init__(self, batches: list[list[TurnEnvelope]]) -> None:
        self._batches = batches
        self.limits: list[int] = []

    async def sweep_due_turns(self, *, limit: int) -> list[TurnEnvelope]:
        self.limits.append(limit)
        if not self._batches:
            return []
        return self._batches.pop(0)


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


async def test_empty_sweep_does_nothing() -> None:
    store = FakeStore([[]])
    delivery = FakeDelivery()
    ops = RecordingOps()
    watchdog = Watchdog(store, delivery, sweep_limit=7, ops=ops)

    assert await watchdog.sweep_once() == []

    assert delivery.enqueued == []
    assert ops.kinds() == []          # 空转不记 watchdog_requeued
    assert store.limits == [7]        # sweep_limit 如实传给存储层


async def test_swept_turns_are_requeued_with_incremented_attempt() -> None:
    swept = [
        make_turn("turn-a", "session-a", attempt=1),
        make_turn("turn-b", "session-b", attempt=3),
    ]
    store = FakeStore([swept])
    delivery = FakeDelivery()
    ops = RecordingOps()
    watchdog = Watchdog(store, delivery, ops=ops)

    requeued = await watchdog.sweep_once()

    assert [(t.turn_id, t.attempt) for t in requeued] == [("turn-a", 2), ("turn-b", 4)]
    assert [(t.turn_id, t.attempt) for t in delivery.enqueued] == [
        ("turn-a", 2),
        ("turn-b", 4),
    ]
    # 除 attempt 外原样重投：payload/context 是 turn 的身份，watchdog 不解释也不改写。
    assert delivery.enqueued[0].payload == swept[0].payload
    assert ops.details("watchdog_requeued") == {
        "turn_ids": ["turn-a", "turn-b"],
        "count": 2,
    }


async def test_one_failed_enqueue_does_not_take_down_the_batch() -> None:
    """单个 turn 投递失败只掉队它自己：批量放弃会把一次失败放大成一整批失声。"""
    store = FakeStore([[make_turn("turn-a"), make_turn("turn-b", "session-b")]])
    delivery = FakeDelivery()
    delivery.fail_on = {"turn-a"}
    ops = RecordingOps()

    requeued = await Watchdog(store, delivery, ops=ops).sweep_once()

    assert [t.turn_id for t in requeued] == ["turn-b"]
    assert [t.turn_id for t in delivery.enqueued] == ["turn-b"]
    assert ops.details("watchdog_requeue_failed")["turn_id"] == "turn-a"
    assert ops.details("watchdog_requeued")["turn_ids"] == ["turn-b"]


async def test_loop_requeues_then_stops_cleanly() -> None:
    store = FakeStore([[make_turn("turn-a", attempt=1)]])
    delivery = FakeDelivery()
    watchdog = Watchdog(store, delivery, interval=0.01)

    watchdog.start()
    for _ in range(200):
        if delivery.enqueued:
            break
        await asyncio.sleep(0.01)
    await watchdog.stop()

    assert [(t.turn_id, t.attempt) for t in delivery.enqueued] == [("turn-a", 2)]
    # stop 之后循环不得再动：再等一会儿 sweep 次数不应继续涨。
    swept_after_stop = len(store.limits)
    await asyncio.sleep(0.05)
    assert len(store.limits) == swept_after_stop
    await watchdog.stop()  # 幂等：重复 stop 不抛


async def test_loop_survives_a_failing_sweep() -> None:
    """一次 sweep 失败不得让 watchdog 停摆——那意味着所有卡住的会话永久失声。"""

    class ExplodingStore(FakeStore):
        def __init__(self) -> None:
            super().__init__([])
            self.calls = 0

        async def sweep_due_turns(self, *, limit: int) -> list[TurnEnvelope]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("store 挂了")
            return [make_turn("turn-late")] if self.calls == 2 else []

    store = ExplodingStore()
    delivery = FakeDelivery()
    ops = RecordingOps()
    watchdog = Watchdog(store, delivery, interval=0.01, ops=ops)

    watchdog.start()
    for _ in range(200):
        if delivery.enqueued:
            break
        await asyncio.sleep(0.01)
    await watchdog.stop()

    assert [t.turn_id for t in delivery.enqueued] == ["turn-late"]
    assert "watchdog_sweep_failed" in ops.kinds()


async def test_stalled_turn_is_left_to_sweep_not_to_delivery_retry(
    state_store, clock: FakeClock
) -> None:
    """停滞 turn 的恢复只有一条路径：pipeline 放手 → 锁过期 → watchdog requeue。

    具体防的是"两条恢复路径"这个 bug 形态：若 pipeline 把 TurnStalledError 外抛，
    InProcessTurnDelivery 会按消费失败自己 attempt+1 重投一次，watchdog 再 requeue
    一次——同一个 turn 被两个发起者各推一遍。
    """
    delivery = InProcessTurnDelivery(max_attempts=1)
    calls: list[int] = []

    async def stalling_runner(turn: TurnEnvelope) -> None:
        calls.append(turn.attempt)
        raise TurnStalledError(turn.turn_id, "liveness")

    processor = TurnProcessor(state_store, stalling_runner, delivery=delivery, lock_seconds=LOCK)
    turn = make_turn()

    await processor.process(turn)   # 正常返回：投递层认为这次投递已被消化

    assert calls == [1]
    assert delivery.dropped == []   # 没有走失败重投 → 没有 dropped
    # 既没 finished 也没 failed：行还在 running 且锁未到期，所以仍占着 session。
    assert await state_store.has_active_turn(turn.session_id) is True

    # 锁按 lock_seconds 自然过期后，sweep 才是唯一接管者。
    clock.advance(LOCK + 1)
    watchdog = Watchdog(state_store, delivery)
    requeued = await watchdog.sweep_once()
    assert [(t.turn_id, t.attempt) for t in requeued] == [(turn.turn_id, 2)]
