"""行编解码：数据库行 <-> 核心类型。

契约见 CONTRACTS.md《核心类型》与《附录 A》表结构。
本模块是纯函数层：不 import sqlite3、不做 IO、不含状态转换语义。输入只要求 Mapping
（sqlite3.Row 满足），因此后续 Postgres 实现可原样复用同一套编解码。

约定：
- payload / context 是库不解释的 opaque dict，落库为 JSON 文本。
- 面向人读的时间戳（updated_at / created_at / finished_at / stamp_bound_at）
  一律 ISO8601 UTC 字符串；locked_until 是参与算术比较的 unix epoch 秒，不经此层。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from ..types import RuntimeStamp, SandboxHandle, TurnEnvelope

__all__ = [
    "dumps_json",
    "loads_json",
    "to_iso",
    "from_iso",
    "utc_now_iso",
    "decode_turn",
    "decode_binding",
    "decode_stamp",
]


def dumps_json(value: Mapping[str, Any]) -> str:
    """opaque dict -> JSON 文本。sort_keys 让落库结果稳定、便于比对。"""
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True)


def loads_json(raw: str) -> dict[str, Any]:
    """JSON 文本 -> opaque dict。"""
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise ValueError("turn 的 payload / context 必须是 JSON object")
    return decoded


def to_iso(moment: datetime) -> str:
    """datetime -> ISO8601 UTC 字符串。naive 输入按 UTC 解释（宿主时钟即 UTC）。"""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def from_iso(raw: str) -> datetime:
    """ISO8601 字符串 -> aware datetime（UTC）。"""
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utc_now_iso() -> str:
    """当前时刻的 ISO8601 UTC 字符串（审计字段用）。"""
    return datetime.now(timezone.utc).isoformat()


def decode_turn(row: Mapping[str, Any]) -> TurnEnvelope:
    """roost_turns 行 -> TurnEnvelope。

    status / locked_until / 时间戳是行状态而非 envelope 内容，解码时丢弃。
    """
    return TurnEnvelope(
        turn_id=row["turn_id"],
        session_id=row["session_id"],
        payload=loads_json(row["payload"]),
        context=loads_json(row["context"]),
        attempt=row["attempt"],
    )


def decode_binding(row: Mapping[str, Any] | None) -> SandboxHandle | None:
    """roost_sessions 行 -> SandboxHandle。行不存在或 sandbox_id 为 NULL 均视为未绑定。"""
    if row is None or row["sandbox_id"] is None:
        return None
    return SandboxHandle(sandbox_id=row["sandbox_id"], backend=row["sandbox_backend"])


def decode_stamp(row: Mapping[str, Any] | None) -> RuntimeStamp | None:
    """roost_sessions 行 -> RuntimeStamp。stamp_bound_at 为 NULL 视为无 stamp。

    template_id / runtime_files_hash 允许为 None（后者 None 表示快照构建期烘焙，
    首次正常重启前豁免比对，见 CONTRACTS.md 核心类型）。
    """
    if row is None or row["stamp_bound_at"] is None:
        return None
    return RuntimeStamp(
        bound_at=from_iso(row["stamp_bound_at"]),
        template_id=row["stamp_template_id"],
        runtime_files_hash=row["stamp_runtime_files_hash"],
    )
