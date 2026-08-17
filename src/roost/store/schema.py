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
    "STATUS_ATTENTION",
    "TERMINAL_STATUSES",
]

# roost_turns.status 的取值空间（附录 A）。与 Terminal.status 是不同值空间，
# 故意不共享枚举；这里只是表结构词汇，状态转换语义在 sqlite.py 的条件 UPDATE 中。
STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"
STATUS_FAILED = "failed"
STATUS_REQUEUED = "requeued"
# 附录 M 对附录 A 终态词表的修订：wall-clock ceiling 触顶的 turn 落 'attention'
# 而不是 'failed'——**需要人工介入**是一个与"失败"不同的事实。沙箱不被销毁、
# 工作区留在原地正是为了让人能进去看；把它记成 failed 会让它混进失败率统计，
# 并让"这个 turn 到底出了什么事"永远查不出来。
STATUS_ATTENTION = "attention"

#: `finish_turn` 允许写入的终态词表。'running' / 'requeued' 归 begin_turn / sweep
#: 所有，禁止外部写入。
TERMINAL_STATUSES = (STATUS_FINISHED, STATUS_FAILED, STATUS_ATTENTION)


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
    finished_at  TEXT NULL,
    error_ordinal INTEGER NOT NULL DEFAULT 0
)
"""

# error_ordinal（附录 M 的升级阶梯计数）是**后加的列**：既有库里的
# `CREATE TABLE IF NOT EXISTS` 不会给它补上，因此建表之后再幂等地 ALTER 一次。
# 它必须持久化——内存里的 marker 会被一次 requeue 清掉，而那正是"同一个沙箱
# 被反复 restart 近百轮"那次事故的机制。
TURNS_ERROR_ORDINAL_COLUMN = "error_ordinal"
TURNS_ERROR_ORDINAL_DDL = (
    f"ALTER TABLE roost_turns ADD COLUMN {TURNS_ERROR_ORDINAL_COLUMN} "
    "INTEGER NOT NULL DEFAULT 0"
)

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
    """在连接上建表建索引（幂等）。调用方负责线程归属与事务语义。

    SQLite 没有 `ADD COLUMN IF NOT EXISTS`，因此后加的列先查 `PRAGMA table_info`
    再决定要不要 ALTER——判据取自**数据库实际状态**，不是"我们这个版本应该有它"。
    """
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(roost_turns)").fetchall()
    }
    if TURNS_ERROR_ORDINAL_COLUMN not in columns:
        conn.execute(TURNS_ERROR_ORDINAL_DDL)
