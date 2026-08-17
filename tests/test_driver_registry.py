"""driver turn registry 状态机单测（I1 driver 侧）。

防的回归：重复提交在**任何**状态下都只读返回既有条目——一旦哪天有人让
duplicate 顺手"刷新 payload"或"重新排队"，exactly-once 就破了，而这类破坏在
端到端测试里往往表现为难查的重复回答。
"""

from __future__ import annotations

import pytest

from conftest import make_turn

from roost.driver.registry import (
    STATE_DONE,
    STATE_QUEUED,
    STATE_RUNNING,
    InvalidTransition,
    TurnRegistry,
    UnknownTurn,
)


def test_first_submit_is_accepted_and_queued() -> None:
    registry = TurnRegistry()
    result = registry.submit(make_turn())

    assert result.accepted is True
    assert result.entry.state == STATE_QUEUED
    assert result.entry.status is None


@pytest.mark.parametrize(
    "advance_to",
    [STATE_QUEUED, STATE_RUNNING, STATE_DONE],
)
def test_duplicate_submit_is_read_only_in_every_state(advance_to: str) -> None:
    registry = TurnRegistry()
    original = make_turn(payload={"text": "first"})
    registry.submit(original)
    if advance_to in (STATE_RUNNING, STATE_DONE):
        registry.mark_running(original.turn_id)
    if advance_to == STATE_DONE:
        registry.mark_done(original.turn_id, status="ok")

    result = registry.submit(make_turn(payload={"text": "second"}, attempt=7))

    assert result.accepted is False
    assert result.entry.state == advance_to
    # 既有条目一字未改：先到的 envelope 是权威。
    assert result.entry.turn is original
    assert len(registry) == 1


def test_lifecycle_transitions_carry_terminal_status() -> None:
    registry = TurnRegistry()
    turn = make_turn()
    registry.submit(turn)

    assert registry.mark_running(turn.turn_id).state == STATE_RUNNING
    done = registry.mark_done(turn.turn_id, status="error")
    assert (done.state, done.status) == (STATE_DONE, "error")
    assert registry.get(turn.turn_id) == done


def test_illegal_transitions_raise() -> None:
    registry = TurnRegistry()
    turn = make_turn()
    registry.submit(turn)

    with pytest.raises(InvalidTransition):
        registry.mark_done(turn.turn_id, status="ok")     # queued -> done 不合法

    registry.mark_running(turn.turn_id)
    with pytest.raises(InvalidTransition):
        registry.mark_running(turn.turn_id)               # running -> running 不合法


def test_unknown_turn_lookup() -> None:
    registry = TurnRegistry()

    assert registry.get("missing") is None
    with pytest.raises(UnknownTurn):
        registry.mark_running("missing")
