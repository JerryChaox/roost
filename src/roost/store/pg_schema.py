"""PostgresStateStore 的表结构（DDL）与 advisory lock 键派生。

契约见 CONTRACTS.md《附录 A：M1 内核契约 — StateStore 最小表结构》与
《附录 L：M11 多消费者与 Postgres 契约》。本模块与 schema.py 平行：只声明结构，
不含任何查询语句与状态转换语义（那些在 postgres.py）。

**类型对齐 PG**（附录 L 授权由实现裁定）：
- 面向人读/审计的时间戳（updated_at / created_at / finished_at / stamp_bound_at）
  用 `TIMESTAMPTZ`——PG 原生带时区语义，胜过 SQLite 侧的 ISO8601 TEXT 权宜。
- `locked_until` 用 `DOUBLE PRECISION`：它是**宿主时钟**的 unix epoch 秒
  （附录 A），要参与算术比较，且刻意不用 PG 的 now()——多实例下时间基准必须
  统一在宿主注入的时钟上，否则锁语义会随各 PG 实例的时钟漂移。
- `payload` / `context` 用 `JSONB`：库不解释其内容，JSONB 只是让运维能查询；
  asyncpg 默认把 jsonb 读回为 str，因此 codec.py 的纯文本编解码可原样复用。

DDL 全部 IF NOT EXISTS，由 store 启动时幂等执行。
"""

from __future__ import annotations

import hashlib

__all__ = [
    "SESSIONS_DDL",
    "TURNS_DDL",
    "SCHEMA_STATEMENTS",
    "session_lock_key",
]


SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS roost_sessions (
    session_id               TEXT PRIMARY KEY,
    sandbox_id               TEXT NULL,
    sandbox_backend          TEXT NULL,
    stamp_bound_at           TIMESTAMPTZ NULL,
    stamp_template_id        TEXT NULL,
    stamp_runtime_files_hash TEXT NULL,
    updated_at               TIMESTAMPTZ NOT NULL
)
"""

TURNS_DDL = """
CREATE TABLE IF NOT EXISTS roost_turns (
    turn_id      TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    status       TEXT NOT NULL,
    payload      JSONB NOT NULL,
    context      JSONB NOT NULL,
    attempt      INTEGER NOT NULL,
    locked_until DOUBLE PRECISION NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL,
    finished_at  TIMESTAMPTZ NULL
)
"""

# 两条热路径各一个索引，与 SQLite 侧一致：has_active_turn 按
# (session_id, status, locked_until)，sweep_due_turns 按 (status, locked_until)。
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


# advisory lock 的键空间是**全库共享的一个 bigint 空间**，因此这里做两件事：
# 1. 加 "roost:session:" 前缀再散列——同库里别的子系统（或 roost 未来别的锁）
#    用同样的 session_id 也不会撞上同一个键；
# 2. 散列在**宿主侧**算，不用 PG 的 hashtext()。hashtext 是内部函数：官方文档
#    从未收录它，历史上改过实现、将来还会改，而 advisory lock 的键必须在滚动
#    升级期间跨版本稳定——否则升级窗口内新旧实例算出不同的键，session 互斥
#    静默失效。blake2b 取 8 字节正好落进 pg_advisory_xact_lock(bigint) 的定义域。
_LOCK_KEY_NAMESPACE = "roost:session:"


def session_lock_key(session_id: str) -> int:
    """session_id -> pg_advisory_xact_lock(bigint) 的键（有符号 64 位）。"""
    digest = hashlib.blake2b(
        (_LOCK_KEY_NAMESPACE + session_id).encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big", signed=True)
