"""I1（sender 侧）：at-least-once 投递下 runner 恰好执行一次。

防的回归：幂等门一旦松动，用户会看到 agent 对同一条消息答复多次——这是本项目
存在的首要理由（DESIGN.md 不变量 I1），且只有并发路径才暴露得出来。
"""

from __future__ import annotations

import asyncio

from conftest import make_turn

from roost import InProcessTurnDelivery, TurnProcessor

DUPLICATES = 8


class CountingRunner:
    """记录执行次数的 fake runner（M3 起换成真实 sandbox 链路）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, turn) -> None:
        self.calls.append(turn.turn_id)
        await asyncio.sleep(0)


async def test_concurrent_duplicate_process_runs_runner_once(state_store) -> None:
    runner = CountingRunner()
    processor = TurnProcessor(state_store, runner, lock_seconds=30)
    turn = make_turn()

    await asyncio.gather(*(processor.process(turn) for _ in range(DUPLICATES)))

    assert runner.calls == [turn.turn_id]


async def test_duplicate_delivery_runs_runner_once(state_store) -> None:
    """投递层人为重复投递 N 份，幂等门只放行一份。"""
    runner = CountingRunner()
    delivery = InProcessTurnDelivery(duplicate_factor=DUPLICATES)
    processor = TurnProcessor(state_store, runner, delivery=delivery, lock_seconds=30)
    delivery.start(processor.process, concurrency=4)
    try:
        await delivery.enqueue(make_turn())
        await delivery.join()
    finally:
        await delivery.stop()

    assert runner.calls == ["turn-1"]


async def test_distinct_turns_in_same_session_all_run(state_store) -> None:
    """串行门只挡并发，不丢件：同 session 的不同 turn 最终都要被执行。"""
    runner = CountingRunner()
    delivery = InProcessTurnDelivery()
    processor = TurnProcessor(
        state_store, runner, delivery=delivery, lock_seconds=30, busy_retry_delay=0.01
    )
    delivery.start(processor.process, concurrency=4)
    try:
        for index in range(4):
            await delivery.enqueue(make_turn(f"turn-{index}"))
        for _ in range(100):
            if len(runner.calls) == 4:
                break
            await asyncio.sleep(0.01)
    finally:
        await delivery.stop()

    assert sorted(runner.calls) == ["turn-0", "turn-1", "turn-2", "turn-3"]
