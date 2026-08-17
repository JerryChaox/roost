"""PostgresStateStore —— StateStore port 的多消费者实现。

契约见 CONTRACTS.md《宿主 ports》StateStore、《附录 A：M1 内核契约》、
《附录 H 增补》sweep 搁浅自愈、《附录 L：M11 多消费者与 Postgres 契约》。
本模块只承担一类职责：把已钉死的状态语义翻译成 SQL 并做 IO 编排。
表结构与 advisory lock 键在 pg_schema.py，行编解码复用 codec.py。

语义与 SQLiteStateStore **逐字相同**（同一套契约套件同时约束两者），差别只有两处，
都是"多消费者"带来的：

1. `session_critical` 是**跨连接、跨进程、跨实例**的事务级 advisory lock
   （`pg_advisory_xact_lock`），而不是进程内 asyncio.Lock。
2. `sweep_due_turns` 用 `FOR UPDATE SKIP LOCKED` 认领——这是附录 A"单事务内选出
   并转 requeued"在 PG 上的正确表达：两个并发 watchdog 各自扫到不相交的一批，
   既不重复认领，也不互相阻塞。

驱动选型：**asyncpg**（可选 extra `roost[postgres]`）。它原生为 asyncio 而生
（不是同步驱动的异步壳），连接池内建在同一个包里，参数走 $n 绑定的二进制协议。
核心零运行时依赖不变：本模块惰性 import asyncpg，未装 extra 时给出清晰的安装指引。

注意：asyncpg 默认会为语句做 server-side prepare，经 pgbouncer 的
transaction pooling 需要把 `statement_cache_size` 设为 0（构造参数透传）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from ..types import RuntimeStamp, SandboxHandle, TurnEnvelope
from .codec import decode_binding, decode_stamp, decode_turn, dumps_json, to_iso
from .pg_schema import SCHEMA_STATEMENTS, session_lock_key
from .schema import STATUS_REQUEUED, STATUS_RUNNING

__all__ = ["PostgresStateStore"]

_MISSING_DRIVER = (
    "PostgresStateStore 需要可选依赖 asyncpg：请安装 `pip install 'roost[postgres]'`"
)


def _load_driver() -> Any:
    """惰性 import 驱动：本模块 import 期零依赖，实例化时才要求 asyncpg 就位。"""
    try:
        import asyncpg
    except ImportError as exc:  # pragma: no cover - 取决于环境是否装了 extra
        raise RuntimeError(_MISSING_DRIVER) from exc
    return asyncpg

_SESSION_COLUMNS = (
    "session_id, sandbox_id, sandbox_backend, stamp_bound_at, "
    "stamp_template_id, stamp_runtime_files_hash, updated_at"
)

_SESSION_ASSIGNMENTS = (
    "sandbox_id = $2, sandbox_backend = $3, stamp_bound_at = $4, "
    "stamp_template_id = $5, stamp_runtime_files_hash = $6, updated_at = $7"
)

_TURN_COLUMNS = (
    "turn_id, session_id, status, payload, context, "
    "attempt, locked_until, created_at, finished_at"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp_view(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """roost_sessions 行 -> codec.decode_stamp 期望的形状。

    唯一的差异是 stamp_bound_at：PG 侧是 TIMESTAMPTZ（读回 datetime），
    codec 的解码入口约定 ISO8601 字符串。在这里对齐，解码语义仍只有一份。
    """
    if row is None:
        return None
    bound_at = row["stamp_bound_at"]
    return {
        "stamp_bound_at": None if bound_at is None else to_iso(bound_at),
        "stamp_template_id": row["stamp_template_id"],
        "stamp_runtime_files_hash": row["stamp_runtime_files_hash"],
    }


class PostgresStateStore:
    """StateStore 的 Postgres 实现（多消费者 source of truth）。

    参数：
        dsn:  postgres 连接串（asyncpg 接受的任意形式）。
        now:  返回 unix epoch 秒的**宿主时钟**，用于 locked_until 的写入与比较。
              刻意不用 PG 的 now()：多实例下时间基准必须统一在宿主侧。
              该注入点存在的唯一理由是让锁过期在测试中可控，生产不应覆盖。
        redelivery_grace_seconds: requeued 行的重投期限（附录 H 增补的搁浅自愈）。
        min_size / max_size: 连接池尺寸。
        statement_cache_size: 透传 asyncpg；经 pgbouncer transaction pooling 时置 0。
    """

    def __init__(
        self,
        dsn: str,
        *,
        now: Callable[[], float] = time.time,
        redelivery_grace_seconds: float = 30.0,
        min_size: int = 1,
        max_size: int = 10,
        statement_cache_size: int | None = None,
    ) -> None:
        if redelivery_grace_seconds <= 0:
            raise ValueError("redelivery_grace_seconds 必须 > 0")
        self._asyncpg = _load_driver()
        self._dsn = dsn
        self._now = now
        self._redelivery_grace_seconds = redelivery_grace_seconds
        self._min_size = min_size
        self._max_size = max_size
        self._statement_cache_size = statement_cache_size
        self._pool: Any = None
        self._pool_lock = asyncio.Lock()
        self._closed = False
        # 临界区持有的连接：同一 task 内的 has_active_turn / begin_turn 必须复用它。
        # 若临界区里的查询另取一条连接，"池被等锁者占满 → 持锁者取不到连接执行
        # begin_turn"就是一个真实的死锁（等锁者全都握着连接在等持锁者提交）。
        self._ambient: ContextVar[Any] = ContextVar(
            f"roost_pg_conn_{id(self):x}", default=None
        )

    # ---- 连接池（IO 边界） ----------------------------------------------------

    async def _ensure_pool(self) -> Any:
        if self._closed:
            raise RuntimeError("PostgresStateStore 已关闭")
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is None:
                kwargs: dict[str, Any] = {
                    "min_size": self._min_size,
                    "max_size": self._max_size,
                }
                if self._statement_cache_size is not None:
                    kwargs["statement_cache_size"] = self._statement_cache_size
                pool = await self._asyncpg.create_pool(self._dsn, **kwargs)
                async with pool.acquire() as conn:
                    for statement in SCHEMA_STATEMENTS:
                        await conn.execute(statement)
                self._pool = pool
        return self._pool

    @asynccontextmanager
    async def _acquire(self) -> AsyncIterator[Any]:
        """取一条连接：临界区内一律复用其持有的那条（见 _ambient 的注释）。"""
        ambient = self._ambient.get()
        if ambient is not None:
            yield ambient
            return
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            yield conn

    async def close(self) -> None:
        """关闭连接池。重复调用是 no-op。"""
        if self._closed:
            return
        self._closed = True
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ---- session 临界区 -------------------------------------------------------

    @asynccontextmanager
    async def session_critical(self, session_id: str) -> AsyncIterator[None]:
        """session 级互斥临界区（附录 L）：包住"串行门检查 + begin_turn"。

        `pg_advisory_xact_lock(bigint)` 是**事务级**的：无手工释放接口，事务
        commit/rollback 即释放——因此临界区不可能因为异常路径而漏放锁。它跨连接、
        跨进程、跨实例生效，这正是多消费者所需。键的派生见 pg_schema.session_lock_key。

        隔离级别**显式钉成 read_committed**，不吃服务端的
        `default_transaction_isolation`：临界区里的串行门读必须看见"抢在我们前面
        拿到锁并提交"的那一行。repeatable read 下事务快照在**取锁之前**就定格了，
        对手提交的行对我们不可见——串行门会双双放行，互斥静默失效。

        持有时间 = 临界区本身（毫秒级）。调用方绝不可把 runner 执行期放进来：
        那会让一条池连接和一个 PG 后端进程被 turn 全程钉死。也不要在临界区内
        spawn task：新 task 会拷走这条连接的引用，而 asyncpg 连接不允许并发使用。
        """
        key = session_lock_key(session_id)
        ambient = self._ambient.get()
        if ambient is not None:
            # 嵌套：已在同一事务里，advisory lock 对同一事务可重入（计数）。
            await ambient.execute("SELECT pg_advisory_xact_lock($1::bigint)", key)
            yield
            return

        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.transaction(isolation="read_committed"):
                await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", key)
                token = self._ambient.set(conn)
                try:
                    yield
                finally:
                    self._ambient.reset(token)

    # ---- 绑定 -----------------------------------------------------------------

    async def get_binding(self, session_id: str) -> SandboxHandle | None:
        async with self._acquire() as conn:
            row = await conn.fetchrow(
                "SELECT sandbox_id, sandbox_backend FROM roost_sessions "
                "WHERE session_id = $1",
                session_id,
            )
        return decode_binding(row)

    async def bind(
        self, session_id: str, sandbox: SandboxHandle, stamp: RuntimeStamp
    ) -> None:
        """无条件绑定（upsert）。需要 CAS 语义时用 swap_binding。"""
        async with self._acquire() as conn:
            await conn.execute(
                f"INSERT INTO roost_sessions ({_SESSION_COLUMNS}) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) "
                "ON CONFLICT (session_id) DO UPDATE SET "
                "    sandbox_id = EXCLUDED.sandbox_id, "
                "    sandbox_backend = EXCLUDED.sandbox_backend, "
                "    stamp_bound_at = EXCLUDED.stamp_bound_at, "
                "    stamp_template_id = EXCLUDED.stamp_template_id, "
                "    stamp_runtime_files_hash = EXCLUDED.stamp_runtime_files_hash, "
                "    updated_at = EXCLUDED.updated_at",
                *self._session_values(session_id, sandbox, stamp),
            )

    async def swap_binding(
        self,
        session_id: str,
        old: SandboxHandle | None,
        new: SandboxHandle,
        stamp: RuntimeStamp,
    ) -> bool:
        """CAS：仅当当前绑定 == old 时换绑到 new（forced update 的原子换绑入口）。

        old is None 表示"仅当当前未绑定"——行不存在（插入）或行存在但
        sandbox_id IS NULL（条件 UPDATE）。CAS 失败时不留下任何写入副作用。
        """
        values = self._session_values(session_id, new, stamp)
        async with self._acquire() as conn:
            if old is None:
                inserted = await conn.fetchval(
                    f"INSERT INTO roost_sessions ({_SESSION_COLUMNS}) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7) "
                    "ON CONFLICT (session_id) DO NOTHING "
                    "RETURNING 1",
                    *values,
                )
                if inserted is not None:
                    return True
                updated = await conn.fetchval(
                    f"UPDATE roost_sessions SET {_SESSION_ASSIGNMENTS} "
                    "WHERE session_id = $1 AND sandbox_id IS NULL "
                    "RETURNING 1",
                    *values,
                )
                return updated is not None

            updated = await conn.fetchval(
                f"UPDATE roost_sessions SET {_SESSION_ASSIGNMENTS} "
                "WHERE session_id = $1 AND sandbox_id = $8 AND sandbox_backend = $9 "
                "RETURNING 1",
                *values,
                old.sandbox_id,
                old.backend,
            )
            return updated is not None

    async def get_stamp(self, session_id: str) -> RuntimeStamp | None:
        async with self._acquire() as conn:
            row = await conn.fetchrow(
                "SELECT stamp_bound_at, stamp_template_id, stamp_runtime_files_hash "
                "FROM roost_sessions WHERE session_id = $1",
                session_id,
            )
        return decode_stamp(_stamp_view(row))

    @staticmethod
    def _session_values(
        session_id: str, sandbox: SandboxHandle, stamp: RuntimeStamp
    ) -> tuple[Any, ...]:
        bound_at = stamp.bound_at
        if bound_at.tzinfo is None:
            bound_at = bound_at.replace(tzinfo=timezone.utc)
        return (
            session_id,
            sandbox.sandbox_id,
            sandbox.backend,
            bound_at,
            stamp.template_id,
            stamp.runtime_files_hash,
            _utc_now(),
        )

    # ---- turn 生命周期 --------------------------------------------------------

    async def has_active_turn(
        self, session_id: str, *, exclude_turn_id: str | None = None
    ) -> bool:
        """session 是否有活跃 turn（session 串行门）。

        锁过期的 running 行**不算** active：wedged turn 的接管只走
        sweep → requeue → 重投递路径，不在这里被当成占位者。
        """
        sql = (
            "SELECT 1 FROM roost_turns "
            "WHERE session_id = $1 AND status = $2 AND locked_until > $3"
        )
        params: list[Any] = [session_id, STATUS_RUNNING, self._now()]
        if exclude_turn_id is not None:
            sql += " AND turn_id <> $4"
            params.append(exclude_turn_id)
        async with self._acquire() as conn:
            found = await conn.fetchval(sql + " LIMIT 1", *params)
        return found is not None

    async def begin_turn(self, turn: TurnEnvelope, *, lock_seconds: int) -> bool:
        """CAS 开始一个 turn（sender 侧幂等门）。

        返回 True 当且仅当：行不存在（插入 running），或行 status='requeued'
        （接管为 running）。行为 running（**无论锁是否过期**）或 finished/failed
        一律 False——begin_turn 自身绝不抢锁。
        """
        locked_until = self._now() + lock_seconds
        payload = dumps_json(turn.payload)
        context = dumps_json(turn.context)
        async with self._acquire() as conn:
            inserted = await conn.fetchval(
                f"INSERT INTO roost_turns ({_TURN_COLUMNS}) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NULL) "
                "ON CONFLICT (turn_id) DO NOTHING "
                "RETURNING 1",
                turn.turn_id,
                turn.session_id,
                STATUS_RUNNING,
                payload,
                context,
                turn.attempt,
                locked_until,
                _utc_now(),
            )
            if inserted is not None:
                return True

            # 只有 requeued 行可被接管。attempt 的唯一 +1 所有者是投递方（重投时
            # 递增），这里只取 GREATEST(行值, envelope 值) 保持单调，绝不自行做
            # 加法——否则同一次重投会被行和 envelope 各记一次。
            taken = await conn.fetchval(
                "UPDATE roost_turns SET "
                "    status = $1, attempt = GREATEST(attempt, $2), locked_until = $3, "
                "    payload = $4, context = $5, finished_at = NULL "
                "WHERE turn_id = $6 AND status = $7 "
                "RETURNING 1",
                STATUS_RUNNING,
                turn.attempt,
                locked_until,
                payload,
                context,
                turn.turn_id,
                STATUS_REQUEUED,
            )
            return taken is not None

    async def renew_turn_lock(self, turn_id: str, *, lock_seconds: int) -> None:
        """心跳续锁。仅作用于 status='running' 的行，其余静默 no-op。"""
        async with self._acquire() as conn:
            await conn.execute(
                "UPDATE roost_turns SET locked_until = $1 "
                "WHERE turn_id = $2 AND status = $3",
                self._now() + lock_seconds,
                turn_id,
                STATUS_RUNNING,
            )

    async def finish_turn(self, turn_id: str, *, status: str) -> None:
        """收尾。仅作用于 status='running' 的行，其余静默 no-op。

        status 只接受终态词汇 'finished' / 'failed'——'running' / 'requeued' 归
        begin_turn / sweep 所有，放行会撞库内状态机（附录 A）。
        """
        if status not in ("finished", "failed"):
            raise ValueError(f"finish_turn 只接受 'finished'/'failed'，得到 {status!r}")

        async with self._acquire() as conn:
            await conn.execute(
                "UPDATE roost_turns SET status = $1, finished_at = $2 "
                "WHERE turn_id = $3 AND status = $4",
                status,
                _utc_now(),
                turn_id,
                STATUS_RUNNING,
            )

    async def sweep_due_turns(self, *, limit: int) -> list[TurnEnvelope]:
        """到期未完成的 turn，转/保持 requeued 并返回，供 watchdog 重投。

        选出与转状态在**单条语句**内完成（CTE + UPDATE ... RETURNING），因此天然
        原子。`FOR UPDATE SKIP LOCKED` 是附录 A"单事务"要求在 PG 上的正确表达：
        并发的第二个 watchdog 跳过已被认领的行去扫下一批，既不重复认领也不排队等。

        返回的 attempt 是行内原值（UPDATE 不动它）；+1 由投递方重投时负责。

        谓词是 `status IN ('running','requeued') AND locked_until <= now`：
        标记 requeued 时同步把 locked_until 推到 now + redelivery_grace——它在
        requeued 状态下的含义是**重投期限**。若 sweep 之后 enqueue 失败或宿主
        崩溃（sweep 与重投之间没有原子性），该行会在期限到期后被再次扫出，
        搁浅自愈；重复重投由 begin_turn 的接管语义吸收（附录 H 增补）。
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(
                "WITH due AS ("
                "    SELECT turn_id FROM roost_turns "
                "     WHERE status IN ($1, $2) AND locked_until <= $3 "
                "     ORDER BY locked_until "
                "     LIMIT $4 "
                "     FOR UPDATE SKIP LOCKED"
                ") "
                "UPDATE roost_turns AS t SET status = $2, locked_until = $5 "
                "  FROM due "
                " WHERE t.turn_id = due.turn_id "
                "RETURNING t.turn_id, t.session_id, t.payload, t.context, t.attempt",
                STATUS_RUNNING,
                STATUS_REQUEUED,
                self._now(),
                limit,
                self._now() + self._redelivery_grace_seconds,
            )
        return [decode_turn(row) for row in rows]
