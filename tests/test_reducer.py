"""reducer 单测（纯函数，无 IO）。

防的回归：四种 driver 事件到 DisplayEvent 的 kind 映射与 body 形状——它是宿主
渲染代码唯一能依赖的契约面；以及 display seq 的平移约定（lifecycle 保留段），
它保证 boot 通告与 driver 事件拼成的单流仍然递增（EventSink port 的承诺）。
"""

from __future__ import annotations

import pytest

from roost import DisplayEvent, LIFECYCLE_SEQ_RESERVED, driver_display_seq, reduce_event, reduce_events
from roost.events import Delta, LifecycleNotice, Terminal, ToolEvent


def test_delta_becomes_text() -> None:
    display = reduce_event(Delta(turn_id="t1", text="hi", seq=3), session_id="s1")

    assert display == DisplayEvent(
        session_id="s1", turn_id="t1", kind="text", body={"text": "hi"}, seq=3
    )


def test_tool_event_becomes_tool() -> None:
    event = ToolEvent(
        turn_id="t1", name="bash", phase="start", detail={"cmd": "ls"}, seq=4
    )

    display = reduce_event(event, session_id="s1")

    assert display.kind == "tool"
    assert display.body == {"name": "bash", "phase": "start", "detail": {"cmd": "ls"}}


def test_lifecycle_notice_keeps_kind_and_elapsed() -> None:
    event = LifecycleNotice(turn_id="t1", kind="boot_finished", elapsed_ms=1200, seq=2)

    display = reduce_event(event, session_id="s1")

    assert display.kind == "lifecycle_notice"
    assert display.body == {"kind": "boot_finished", "elapsed_ms": 1200}


def test_terminal_carries_status_error_usage() -> None:
    event = Terminal(
        turn_id="t1", status="error", error="boom", usage={"tokens": 7}, seq=9
    )

    display = reduce_event(event, session_id="s1")

    assert display.kind == "terminal"
    assert display.body == {"status": "error", "error": "boom", "usage": {"tokens": 7}}


def test_body_never_repeats_promoted_fields() -> None:
    display = reduce_event(Delta(turn_id="t1", text="hi", seq=3), session_id="s1")

    assert "turn_id" not in display.body
    assert "seq" not in display.body


def test_reduce_events_offsets_past_lifecycle_block() -> None:
    events = [
        Delta(turn_id="t1", text="a", seq=1),
        Terminal(turn_id="t1", status="ok", error=None, usage={}, seq=2),
    ]

    display = reduce_events(events, session_id="s1")

    assert [d.seq for d in display] == [
        driver_display_seq(1),
        driver_display_seq(2),
    ]
    # boot 通告占 1..LIFECYCLE_SEQ_RESERVED，driver 事件必须排在其后。
    assert display[0].seq > LIFECYCLE_SEQ_RESERVED


def test_reduce_events_can_keep_source_seq() -> None:
    events = [Delta(turn_id="t1", text="a", seq=1)]

    assert reduce_events(events, session_id="s1", offset=False)[0].seq == 1


def test_unknown_event_type_raises() -> None:
    with pytest.raises(TypeError):
        reduce_event(object(), session_id="s1")  # type: ignore[arg-type]
