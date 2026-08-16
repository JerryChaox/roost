"""StateStore 表结构（DDL）。

契约见 CONTRACTS.md《附录 A：M1 内核契约 — StateStore 最小表结构》。
本模块只承担一类职责：声明表结构并把它应用到连接上。不含任何查询语句与状态转换语义。

表结构刻意保持 SQLite / Postgres 中性：不使用 SQLite 特有类型，时间戳分两种表达——
ISO8601 UTC 字符串（人读、审计）与 unix epoch 秒的 REAL（锁比较，需参与算术）。
"""

from __future__ import annotations

import sqlite3

__all__ = [
    "SESSIONS_DDL",
    "TURNS_DDL",
    "SCHEMA_STATEMENTS",
    "apply_schema",
    "STATUS_RUNNING",
    "STATUS_FINISHED",
    "STATUS_FAILED",
    "STATUS_REQUEUED",
]

# roost_turns.status 的取值空间（附录 A）。与 Terminal.status 是不同值空间，
# 故意不共享枚举；这里只是表结构词汇，状态转换语义在 sqlite.py 的条件 UPDATE 中。
STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"
STATUS_FAILED = "failed"
STATUS_REQUEUED = "requeued"


SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS roost_sessions (
    session_id               TEXT PRIMARY KEY,
    sandbox_id               TEXT NULL,
    sandbox_backend          TEXT NULL,
    stamp_bound_at           TEXT NULL,
    stamp_template_id        TEXT NULL,
    stamp_runtime_files_hash TEXT NULL,
    updated_at               TEXT NOT NULL
)
"""

TURNS_DDL = """
CREATE TABLE IF NOT EXISTS roost_turns (
    turn_id      TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    status       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    context      TEXT NOT NULL,
    attempt      INTEGER NOT NULL,
    locked_until REAL NOT NULL,
    created_at   TEXT NOT NULL,
    finished_at  TEXT NULL
)
"""

# 两条热路径各一个索引：has_active_turn 按 (session_id, status, locked_until)，
# sweep_due_turns 按 (status, locked_until)。
TURNS_SESSION_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS roost_turns_session_status_idx
    ON roost_turns (session_id, status, locked_until)
"""

TURNS_SWEEP_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS roost_turns_status_locked_idx
    ON roost_turns (status, locked_until)
"""

SCHEMA_STATEMENTS: tuple[str, ...] = (
    SESSIONS_DDL,
    TURNS_DDL,
    TURNS_SESSION_INDEX_DDL,
    TURNS_SWEEP_INDEX_DDL,
)


def apply_schema(conn: sqlite3.Connection) -> None:
    """在连接上建表建索引（幂等）。调用方负责线程归属与事务语义。"""
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
