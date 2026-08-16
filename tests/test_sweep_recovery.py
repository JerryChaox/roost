"""sweep 恢复：wedged turn 被 requeue 后重跑成功。

防的回归：宿主在 runner 执行中途崩溃时，turn 行会停在 running 且锁不再续期。
若 sweep→requeue→重投递这条链断掉，用户的消息就永远没有答复（静默丢件）。
requeue 是**显式允许**的重跑：总执行次数为 2 是契约，不是 bug。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from conftest import FakeClock, make_turn

from roost import TurnProcessor

LOCK = 2


class RecordingRunner:
    """第一次调用挂死（模拟 wedged turn），之后正常返回。"""

    def __init__(self) -> None:
        self.calls: list[int] = []
        self.started = asyncio.Event()

    async def __call__(self, turn) -> None:
        self.calls.append(turn.attempt)
        self.started.set()
        if len(self.calls) == 1:
            await asyncio.Event().wait()  # 永不返回


async def test_wedged_turn_is_swept_requeued_and_rerun(
    state_store, clock: FakeClock
) -> None:
    runner = RecordingRunner()
    processor = TurnProcessor(state_store, runner, lock_seconds=LOCK)
    turn = make_turn()

    # 第一次执行：runner 挂死。
    task = asyncio.ensure_future(processor.process(turn))
    await asyncio.wait_for(runner.started.wait(), timeout=1)

    # 模拟宿主崩溃：取消处理任务，心跳随之停止，且刻意不收尾（锁留待过期）。
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    # 锁到期前，wedged turn 仍占着 session；到期后不再算 active。
    assert await state_store.has_active_turn(turn.session_id) is True
    clock.advance(LOCK + 1)
    assert await state_store.has_active_turn(turn.session_id) is False

    # watchdog：扫出到期行并转 requeued。
    swept = await state_store.sweep_due_turns(limit=10)
    assert [t.turn_id for t in swept] == [turn.turn_id]
    assert swept[0].attempt == 1

    # 投递方重投时 attempt+1。
    redelivered = replace(swept[0], attempt=swept[0].attempt + 1)
    await processor.process(redelivered)

    # 恰好重跑一次，且第二次跑的是 attempt=2 的信封。
    assert runner.calls == [1, 2]

    # 终态 finished：不能再被 begin_turn 接管，也不会再被扫出来。
    assert await state_store.begin_turn(redelivered, lock_seconds=LOCK) is False
    clock.advance(LOCK + 1)
    assert await state_store.sweep_due_turns(limit=10) == []


async def test_failed_runner_finishes_without_requeue(
    state_store, clock: FakeClock
) -> None:
    """runner 抛异常是终态：记 failed，不留给 sweep 反复重跑。"""
    calls: list[str] = []

    async def failing_runner(turn) -> None:
        calls.append(turn.turn_id)
        raise RuntimeError("boom")

    processor = TurnProcessor(state_store, failing_runner, lock_seconds=LOCK)
    turn = make_turn()

    await processor.process(turn)  # 异常被吞，不外抛

    assert calls == [turn.turn_id]
    clock.advance(LOCK + 1)
    assert await state_store.sweep_due_turns(limit=10) == []
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is False
