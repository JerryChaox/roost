"""后加列的幂等演进：既有库必须拿到 `error_ordinal`（CONTRACTS.md 附录 M）。

防的回归：`CREATE TABLE IF NOT EXISTS` 对**已经存在**的表什么都不做。M12 之前
建的库里没有 error_ordinal 这一列，升级后第一次 bump 会直接报 no such column——
一次卡死判定当场变成一个未捕获异常，恢复路径连走都走不到。演进策略是
expand-only（只加列、带默认值、不回填），因此判据只有一个：**旧库也能用**。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from conftest import FakeClock, make_turn

from roost import SQLiteStateStore

LEGACY_TURNS_DDL = """
CREATE TABLE roost_turns (
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


async def test_sqlite_adds_the_column_to_a_legacy_database(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(LEGACY_TURNS_DDL)
    conn.commit()
    conn.close()

    store = SQLiteStateStore(path, now=FakeClock())
    try:
        turn = make_turn()
        assert await store.begin_turn(turn, lock_seconds=30) is True
        assert await store.bump_error_ordinal(turn.turn_id) == 1
    finally:
        await store.close()


#: M11 期的 PG 表结构，逐字保留（少的正是 error_ordinal 这一列）。
LEGACY_PG_TURNS_DDL = """
CREATE TABLE roost_turns (
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


async def test_postgres_adds_the_column_to_a_legacy_database(postgres_dsn) -> None:
    asyncpg = pytest.importorskip("asyncpg")

    conn = await asyncpg.connect(postgres_dsn)
    try:
        await conn.execute("DROP TABLE IF EXISTS roost_turns")
        await conn.execute("DROP TABLE IF EXISTS roost_sessions")
        await conn.execute(LEGACY_PG_TURNS_DDL)
    finally:
        await conn.close()

    from roost import PostgresStateStore

    store = PostgresStateStore(postgres_dsn, now=FakeClock())
    try:
        turn = make_turn()
        assert await store.begin_turn(turn, lock_seconds=30) is True
        assert await store.bump_error_ordinal(turn.turn_id) == 1
    finally:
        await store.close()
