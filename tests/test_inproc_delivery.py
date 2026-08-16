"""InProcessTurnDelivery 的投递语义。

防的回归：at-least-once 的三个承诺——人为重复投递可注入、消费失败会重投且
attempt 递增、not_before 延后生效。这三条是上层幂等测试的前提，前提塌了，
幂等测试会变成"什么都没验证"的绿灯。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from conftest import make_turn

from roost import InProcessTurnDelivery


async def _drain(delivery: InProcessTurnDelivery) -> None:
    await delivery.join()
    await delivery.stop()


async def test_duplicate_factor_delivers_multiple_copies() -> None:
    seen: list[str] = []

    async def handler(turn) -> None:
        seen.append(turn.turn_id)

    delivery = InProcessTurnDelivery(duplicate_factor=3)
    delivery.start(handler)
    await delivery.enqueue(make_turn())
    await _drain(delivery)

    assert seen == ["turn-1"] * 3


async def test_failed_handler_is_redelivered_with_incremented_attempt() -> None:
    attempts: list[int] = []

    async def handler(turn) -> None:
        attempts.append(turn.attempt)
        if turn.attempt < 3:
            raise RuntimeError("transient")

    delivery = InProcessTurnDelivery(max_attempts=5)
    delivery.start(handler)
    await delivery.enqueue(make_turn())
    await _drain(delivery)

    assert attempts == [1, 2, 3]


async def test_redelivery_stops_at_max_attempts() -> None:
    async def always_failing(turn) -> None:
        raise RuntimeError("permanent")

    delivery = InProcessTurnDelivery(max_attempts=3)
    delivery.start(always_failing)
    await delivery.enqueue(make_turn())
    await _drain(delivery)

    assert [t.attempt for t in delivery.dropped] == [3]


async def test_not_before_defers_delivery() -> None:
    seen: list[str] = []

    async def handler(turn) -> None:
        seen.append(turn.turn_id)

    delivery = InProcessTurnDelivery()
    delivery.start(handler)
    not_before = datetime.now(timezone.utc) + timedelta(seconds=0.15)
    await delivery.enqueue(make_turn(), not_before=not_before)
    try:
        await asyncio.sleep(0.05)
        assert seen == []  # 还没到点
        await asyncio.sleep(0.2)
        assert seen == ["turn-1"]
    finally:
        await delivery.stop()
