"""并发 sweep 不重复认领（附录 A 的"单事务"要求，PG 侧靠 FOR UPDATE SKIP LOCKED）。

防的回归：两个 watchdog（多消费者部署的常态）同时 sweep 时，若"选出"与"转
requeued"之间可被交错，同一个 turn 会被两边各认领一次 → 两次重投 → 同一条消息
被执行两次。这正是 I1 要挡的失效，且只在并发下暴露。

断言的是外部可观察性质：并发 sweep 的返回集合两两不相交，且并集恰好是全部到期行。
"""

from __future__ import annotations

import asyncio

from conftest import FakeClock, make_turn

LOCK = 30
DUE = 8


async def test_concurrent_sweeps_claim_disjoint_sets(
    state_store, clock: FakeClock
) -> None:
    for index in range(DUE):
        assert await state_store.begin_turn(
            make_turn(f"turn-{index}", session_id=f"session-{index}"),
            lock_seconds=LOCK,
        )
    clock.advance(LOCK + 1)

    batches = await asyncio.gather(
        *(state_store.sweep_due_turns(limit=DUE) for _ in range(4))
    )

    claimed = [turn.turn_id for batch in batches for turn in batch]
    assert sorted(claimed) == sorted(f"turn-{index}" for index in range(DUE)), batches
