"""wire 编解码 round-trip（TurnEnvelope + 四种 DriverEvent）。

防的回归：编解码是 host 与 driver **共用的同一份实现**，它一旦不对称（某个字段
只在一端读写、判别字段写错、null 被吃掉），故障表现是跨进程的、在端到端测试里
只会显示成"事件少了一条"。这里用 dict / bytes 两条路径各走一遍闭环把它钉住。
"""

from __future__ import annotations

import pytest

from conftest import make_turn

from roost import decode_event, decode_turn, encode_event, encode_turn
from roost.control.envelope import (
    EVENT_TYPES,
    ProtocolError,
    decode_event_bytes,
    decode_turn_bytes,
    encode_event_bytes,
    encode_turn_bytes,
)
from roost.events import Delta, LifecycleNotice, Terminal, ToolEvent

EVENTS = [
    Delta(turn_id="t-1", text="你好 world", seq=1),
    ToolEvent(
        turn_id="t-1",
        name="bash",
        phase="result",
        detail={"exit_code": 0, "stdout": "ok", "nested": {"a": [1, 2]}},
        seq=2,
    ),
    LifecycleNotice(turn_id="t-1", kind="boot_finished", elapsed_ms=1234, seq=3),
    Terminal(turn_id="t-1", status="error", error=None, usage={}, seq=4),
    Terminal(
        turn_id="t-1", status="ok", error="ignored", usage={"tokens": 7}, seq=5
    ),
]

TURNS = [
    make_turn(),
    make_turn(turn_id="t-2", payload={}, context={"trace": "abc"}, attempt=3),
    make_turn(
        turn_id="🐦-unicode",
        payload={"text": "多行\n文本", "nested": {"list": [1, "2", None, True]}},
    ),
]


def test_every_event_type_has_a_discriminator() -> None:
    assert set(EVENT_TYPES) == {"delta", "tool_event", "lifecycle_notice", "terminal"}
    assert {encode_event(event)["type"] for event in EVENTS} == set(EVENT_TYPES)


@pytest.mark.parametrize("event", EVENTS, ids=lambda e: type(e).__name__ + str(e.seq))
def test_event_round_trip(event) -> None:
    assert decode_event(encode_event(event)) == event
    assert decode_event_bytes(encode_event_bytes(event)) == event


@pytest.mark.parametrize("turn", TURNS, ids=lambda t: t.turn_id)
def test_turn_round_trip(turn) -> None:
    assert decode_turn(encode_turn(turn)) == turn
    assert decode_turn_bytes(encode_turn_bytes(turn)) == turn


def test_turn_defaults_apply_when_optional_fields_absent() -> None:
    turn = decode_turn({"turn_id": "t-9", "session_id": "s-9", "payload": {"a": 1}})

    assert (turn.context, turn.attempt) == ({}, 1)


@pytest.mark.parametrize(
    "raw",
    [
        {"session_id": "s", "payload": {}},                       # 缺 turn_id
        {"turn_id": "", "session_id": "s", "payload": {}},         # 空 turn_id
        {"turn_id": "t", "session_id": "s"},                       # 缺 payload
        {"turn_id": "t", "session_id": "s", "payload": []},        # payload 非 object
        {"turn_id": "t", "session_id": "s", "payload": {}, "attempt": True},
        ["not", "an", "object"],
    ],
)
def test_invalid_turn_shapes_rejected(raw) -> None:
    with pytest.raises(ProtocolError):
        decode_turn(raw)


@pytest.mark.parametrize(
    "raw",
    [
        {"turn_id": "t", "seq": 1, "text": "x"},                   # 缺 type
        {"type": "nope", "turn_id": "t", "seq": 1},                # 未知 type
        {"type": "delta", "turn_id": "t", "seq": "1", "text": "x"},  # seq 非整数
        {"type": "delta", "turn_id": "t", "seq": 1},               # 缺 text
        {"type": "terminal", "turn_id": "t", "seq": 1, "status": "ok", "usage": {}},
    ],
)
def test_invalid_event_shapes_rejected(raw) -> None:
    with pytest.raises(ProtocolError):
        decode_event(raw)


def test_invalid_bytes_rejected() -> None:
    with pytest.raises(ProtocolError):
        decode_turn_bytes(b"{not json")
    with pytest.raises(ProtocolError):
        decode_event_bytes(b"\xff\xfe")
