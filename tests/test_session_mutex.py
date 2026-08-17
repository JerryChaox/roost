"""session 临界区：同 session 的两个不同 turn 永不并发执行（附录 L 验收）。

防的回归：串行门（has_active_turn）与幂等门（begin_turn）是两次独立 CAS。M1 只在
单消费者下安全——多 worker 时两个**不同** turn 的检查可以交错，双双越过串行门，
于是同一个沙箱会被两个 turn 同时驱动（会话记忆互相踩、事件流交叉）。这类失效
是静默的、且只在并发下暴露，类型检查与单消费者用例都看不见。

用 fake runner 记录每次执行的 [进入, 离开] 区间，断言任意两个区间不重叠。
SQLite（进程内 asyncio.Lock）与 Postgres（pg_advisory_xact_lock）两个实现都必须过；
Postgres 额外用**两个独立 store 实例**再跑一遍——advisory lock 的价值就在于跨连接
生效，同一个 store 内的池连接与另一个 store 的池连接必须互相看得见对方的锁。
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import (
    FakeClock,
    make_postgres_store,
    make_turn,
    reset_postgres,
)

from roost import InProcessTurnDelivery, TurnProcessor

CONCURRENCY = 4
ROUNDS = 12
LOCK = 30


class IntervalRunner:
    """记录每次执行的区间 [进入 tick, 离开 tick]；执行期间刻意让出事件循环。

    hold > 0 时在执行中真等一小会儿：并发确实发生时区间必然重叠，用来验证
    "锁粒度没被放大到跨 session"这类反向断言不至于因调度巧合而假过。
    """

    def __init__(self, *, hold: float = 0.0) -> None:
        self.intervals: list[tuple[int, str, int]] = []
        self._hold = hold
        self._tick = 0

    def _next(self) -> int:
        self._tick += 1
        return self._tick

    async def __call__(self, turn) -> None:
        enter = self._next()
        await asyncio.sleep(self._hold)
        await asyncio.sleep(0)
        self.intervals.append((enter, turn.turn_id, self._next()))


def assert_never_overlaps(runner: IntervalRunner, expected: int) -> None:
    assert len(runner.intervals) == expected, runner.intervals
    ordered = sorted(runner.intervals)
    for (_, first_id, first_end), (second_start, second_id, _) in zip(
        ordered, ordered[1:]
    ):
        assert first_end < second_start, (
            f"{first_id} 与 {second_id} 的执行区间重叠：{runner.intervals}"
        )


async def _drive(
    store, runner: IntervalRunner, turns, *, expected_total: int | None = None
) -> None:
    """concurrency=4 的投递上并发跑一批 turn，跑完为止。

    expected_total 用于多个 _drive 共用一个 runner 的场景：每个 drive 都必须等到
    **总数**齐了才关投递，否则先跑完的一方会把还在延后重投的 turn 一起停掉。
    """
    delivery = InProcessTurnDelivery()
    processor = TurnProcessor(
        store, runner, delivery=delivery, lock_seconds=LOCK, busy_retry_delay=0.001
    )
    delivery.start(processor.process, concurrency=CONCURRENCY)
    try:
        for turn in turns:
            await delivery.enqueue(turn)
        target = len(turns) if expected_total is None else expected_total
        deadline = asyncio.get_running_loop().time() + 30
        while len(runner.intervals) < target:
            if asyncio.get_running_loop().time() > deadline:
                pytest.fail(f"turn 未在超时内全部执行：{runner.intervals}")
            await asyncio.sleep(0.005)
    finally:
        await delivery.stop()


async def test_same_session_turns_never_overlap(state_store) -> None:
    """两个实现共用的验收：同 session 的不同 turn 反复并发投递，区间永不重叠。

    hold > 0 是有意的：runner 执行期必须比一次 store 往返更长，否则"先到的 turn
    早已跑完"会掩盖互斥缺失（PG 往返慢，零 hold 时这条断言在无锁实现下也可能过）。
    """
    runner = IntervalRunner(hold=0.02)
    turns = [make_turn(f"turn-{index}", session_id="session-1") for index in range(ROUNDS)]

    await _drive(state_store, runner, turns)

    assert_never_overlaps(runner, ROUNDS)
    assert sorted(t[1] for t in runner.intervals) == sorted(t.turn_id for t in turns)


async def test_other_sessions_are_not_serialized(state_store) -> None:
    """临界区只锁本 session：不同 session 的 turn 必须能并发（锁粒度没被放大）。"""
    runner = IntervalRunner(hold=0.05)
    turns = [make_turn(f"turn-{index}", session_id=f"session-{index}") for index in range(4)]

    await _drive(state_store, runner, turns)

    assert len(runner.intervals) == 4
    # 至少有一对区间重叠——否则说明临界区退化成了全局串行。
    ordered = sorted(runner.intervals)
    assert any(
        first_end > second_start
        for (_, _, first_end), (second_start, _, _) in zip(ordered, ordered[1:])
    ), runner.intervals


async def test_postgres_mutex_spans_separate_store_instances(
    postgres_dsn: str, clock: FakeClock
) -> None:
    """PG advisory lock 跨连接/跨实例生效：两个独立 store 各带自己的连接池。"""
    await reset_postgres(postgres_dsn)
    first = await make_postgres_store(postgres_dsn, clock)
    second = await make_postgres_store(postgres_dsn, clock)
    runner = IntervalRunner()
    turns = [make_turn(f"turn-{index}", session_id="session-1") for index in range(ROUNDS)]
    try:
        await asyncio.gather(
            _drive(first, runner, turns[: ROUNDS // 2], expected_total=ROUNDS),
            _drive(second, runner, turns[ROUNDS // 2 :], expected_total=ROUNDS),
        )
    finally:
        await first.close()
        await second.close()

    assert_never_overlaps(runner, ROUNDS)
