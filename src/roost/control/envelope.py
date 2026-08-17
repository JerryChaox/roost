"""Wire 编解码 —— 纯函数，无 IO。

契约见 CONTRACTS.md《附录 B — Wire 约定》：

- 编码 JSON / UTF-8；
- TurnEnvelope 的 wire 形状 = `types.py` 字段原名；
- DriverEvent 的 wire 形状 = dataclass 字段 + 判别字段 `"type"`
  （"delta" | "tool_event" | "lifecycle_notice" | "terminal"）。

本模块是 **双端共用的同一实现**：host 侧 `control/client.py` 与沙箱侧
`driver/server.py` 都只经这里进出 wire，任何一端都不得再写第二份编解码。
职责边界：这里只做 dict/bytes <-> dataclass 的翻译与形状校验，不碰 HTTP、
不碰状态机、不做任何 IO。非法输入一律抛 `ProtocolError`（`ValueError` 子类），
由调用方翻译成 400。
"""

from __future__ import annotations

import json
from typing import Any

from ..events import Delta, DriverEvent, LifecycleNotice, Terminal, ToolEvent
from ..types import TurnEnvelope

__all__ = [
    "EVENT_TYPES",
    "ProtocolError",
    "decode_json",
    "encode_json",
    "encode_turn",
    "decode_turn",
    "encode_turn_bytes",
    "decode_turn_bytes",
    "encode_event",
    "decode_event",
    "encode_event_bytes",
    "decode_event_bytes",
]


class ProtocolError(ValueError):
    """wire 数据不符合协议形状。"""


# 判别字段取值 -> dataclass。新增事件类型时**只**改这张表。
_EVENT_TYPE_TO_CLASS: dict[str, type] = {
    "delta": Delta,
    "tool_event": ToolEvent,
    "lifecycle_notice": LifecycleNotice,
    "terminal": Terminal,
}
_CLASS_TO_EVENT_TYPE: dict[type, str] = {
    cls: name for name, cls in _EVENT_TYPE_TO_CLASS.items()
}

EVENT_TYPES: tuple[str, ...] = tuple(_EVENT_TYPE_TO_CLASS)


# ---- 取值助手（形状校验集中在这里，各字段读取不重复写 isinstance） -------------


def _field(obj: dict[str, Any], name: str) -> Any:
    if name not in obj:
        raise ProtocolError(f"缺少字段 {name!r}")
    return obj[name]


def _as_str(obj: dict[str, Any], name: str) -> str:
    value = _field(obj, name)
    if not isinstance(value, str):
        raise ProtocolError(f"字段 {name!r} 必须是 string")
    return value


def _as_opt_str(obj: dict[str, Any], name: str) -> str | None:
    value = _field(obj, name)
    if value is not None and not isinstance(value, str):
        raise ProtocolError(f"字段 {name!r} 必须是 string 或 null")
    return value


def _as_int(obj: dict[str, Any], name: str, *, default: int | None = None) -> int:
    if name not in obj and default is not None:
        return default
    value = _field(obj, name)
    # bool 是 int 的子类，但 wire 上出现 true/false 是形状错误。
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"字段 {name!r} 必须是 integer")
    return value


def _as_object(
    obj: dict[str, Any], name: str, *, default: dict[str, Any] | None = None
) -> dict[str, Any]:
    if name not in obj and default is not None:
        return dict(default)
    value = _field(obj, name)
    if not isinstance(value, dict):
        raise ProtocolError(f"字段 {name!r} 必须是 object")
    return value


def _as_mapping(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProtocolError("wire 顶层必须是 JSON object")
    return raw


def _loads(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"非法 JSON/UTF-8：{exc}") from exc


def _dumps(obj: Any) -> bytes:
    try:
        return json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"无法序列化为 JSON：{exc}") from exc


def decode_json(data: bytes) -> Any:
    """UTF-8 JSON bytes -> Python 对象；非法输入抛 ProtocolError。"""
    return _loads(data)


def encode_json(obj: Any) -> bytes:
    """Python 对象 -> UTF-8 JSON bytes；不可序列化时抛 ProtocolError。"""
    return _dumps(obj)


# ---- TurnEnvelope ------------------------------------------------------------


def encode_turn(turn: TurnEnvelope) -> dict[str, Any]:
    """TurnEnvelope -> wire dict（字段原名）。"""
    return {
        "turn_id": turn.turn_id,
        "session_id": turn.session_id,
        "payload": turn.payload,
        "context": turn.context,
        "attempt": turn.attempt,
    }


def decode_turn(raw: Any) -> TurnEnvelope:
    """wire dict -> TurnEnvelope。`context` / `attempt` 缺省时取 dataclass 默认值。"""
    obj = _as_mapping(raw)
    turn_id = _as_str(obj, "turn_id")
    if not turn_id:
        raise ProtocolError("turn_id 不得为空")
    return TurnEnvelope(
        turn_id=turn_id,
        session_id=_as_str(obj, "session_id"),
        payload=_as_object(obj, "payload"),
        context=_as_object(obj, "context", default={}),
        attempt=_as_int(obj, "attempt", default=1),
    )


def encode_turn_bytes(turn: TurnEnvelope) -> bytes:
    return _dumps(encode_turn(turn))


def decode_turn_bytes(data: bytes) -> TurnEnvelope:
    return decode_turn(_loads(data))


# ---- DriverEvent -------------------------------------------------------------


def encode_event(event: DriverEvent) -> dict[str, Any]:
    """DriverEvent -> wire dict（dataclass 字段 + 判别字段 "type"）。"""
    event_type = _CLASS_TO_EVENT_TYPE.get(type(event))
    if event_type is None:
        raise ProtocolError(f"未知事件类型 {type(event).__name__}")

    if isinstance(event, Delta):
        body: dict[str, Any] = {"text": event.text}
    elif isinstance(event, ToolEvent):
        body = {"name": event.name, "phase": event.phase, "detail": event.detail}
    elif isinstance(event, LifecycleNotice):
        body = {"kind": event.kind, "elapsed_ms": event.elapsed_ms}
    else:  # Terminal
        body = {"status": event.status, "error": event.error, "usage": event.usage}

    return {"type": event_type, "turn_id": event.turn_id, "seq": event.seq, **body}


def decode_event(raw: Any) -> DriverEvent:
    """wire dict -> DriverEvent（按 "type" 判别）。"""
    obj = _as_mapping(raw)
    event_type = _as_str(obj, "type")
    if event_type not in _EVENT_TYPE_TO_CLASS:
        raise ProtocolError(f"未知事件 type {event_type!r}")

    turn_id = _as_str(obj, "turn_id")
    seq = _as_int(obj, "seq")

    if event_type == "delta":
        return Delta(turn_id=turn_id, text=_as_str(obj, "text"), seq=seq)
    if event_type == "tool_event":
        return ToolEvent(
            turn_id=turn_id,
            name=_as_str(obj, "name"),
            phase=_as_str(obj, "phase"),
            detail=_as_object(obj, "detail"),
            seq=seq,
        )
    if event_type == "lifecycle_notice":
        return LifecycleNotice(
            turn_id=turn_id,
            kind=_as_str(obj, "kind"),
            elapsed_ms=_as_int(obj, "elapsed_ms"),
            seq=seq,
        )
    return Terminal(
        turn_id=turn_id,
        status=_as_str(obj, "status"),
        error=_as_opt_str(obj, "error"),
        usage=_as_object(obj, "usage"),
        seq=seq,
    )


def encode_event_bytes(event: DriverEvent) -> bytes:
    return _dumps(encode_event(event))


def decode_event_bytes(data: bytes) -> DriverEvent:
    return decode_event(_loads(data))
