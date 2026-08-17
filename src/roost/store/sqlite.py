"""SQLiteStateStore —— StateStore port 的默认实现。

契约见 CONTRACTS.md《宿主 ports》StateStore 与《附录 A：M1 内核契约》。
本模块只承担一类职责：把已钉死的状态语义翻译成 SQL 并做 IO 编排。
行编解码在 codec.py，表结构在 schema.py。

两条实现约束（附录 A）：

1. **全部状态转换用单语句条件 UPDATE 表达 CAS**，靠 rowcount 判定成败，
   不依赖跨语句的事务隔离级别。唯一的多语句事务是 sweep_due_turns
   （选出 + 转 requeued 必须原子，否则两个 watchdog 会重复认领同一批）。
2. **单写者进程假设**：标准库 sqlite3 是同步的，本实现把一个连接钉在专属的单
   worker 线程上，因此库内所有访问天然串行化。跨进程并发写不在 SQLite 默认实现的
   承诺范围内——需要多写者请用后续里程碑的 Postgres 实现。

零运行时依赖：只用标准库 sqlite3 + concurrent.futures。async 接口通过
`loop.run_in_executor(单线程 executor)` 包装同步调用；刻意不用 asyncio.to_thread，
那是共享线程池，会让连接在多个线程间漂移（sqlite3 连接默认绑定创建它的线程）。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from ..types import RuntimeStamp, SandboxHandle, TurnEnvelope
from .codec import (
    decode_binding,
    decode_stamp,
    decode_turn,
    dumps_json,
    to_iso,
    utc_now_iso,
)
from .schema import (
    STATUS_REQUEUED,
    STATUS_RUNNING,
    TERMINAL_STATUSES,
    apply_schema,
)

__all__ = ["SQLiteStateStore"]

_T = TypeVar("_T")

_SESSION_COLUMNS = (
    "session_id, sandbox_id, sandbox_backend, stamp_bound_at, "
    "stamp_template_id, stamp_runtime_files_hash, updated_at"
)

_SESSION_ASSIGNMENTS = (
    "sandbox_id = ?, sandbox_backend = ?, stamp_bound_at = ?, "
    "stamp_template_id = ?, stamp_runtime_files_hash = ?, updated_at = ?"
)

_TURN_COLUMNS = (
    "turn_id, session_id, status, payload, context, "
    "attempt, locked_until, created_at, finished_at"
)


class SQLiteStateStore:
    """StateStore 的 SQLite 实现（source of truth）。

    参数：
        path: 数据库文件路径；None 或 ":memory:" 使用内存库（测试用）。
        now:  返回 unix epoch 秒的时钟，用于 locked_until 的写入与比较。
              该注入点存在的唯一理由是让锁过期在测试中可控，生产不应覆盖。
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        now: Callable[[], float] = time.time,
        redelivery_grace_seconds: float = 30.0,
    ) -> None:
        if redelivery_grace_seconds <= 0:
            raise ValueError("redelivery_grace_seconds 必须 > 0")
        self._path = ":memory:" if path is None else os.fspath(path)
        self._now = now
        self._redelivery_grace_seconds = redelivery_grace_seconds
        self._conn: sqlite3.Connection | None = None
        self._closed = False
        # session_id -> (asyncio.Lock, 等待者+持有者计数)。计数归零即回收，
        # 避免长期运行的宿主按 session 无限堆积锁对象。
        self._session_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="roost-sqlite-store"
        )

    # ---- 连接与调度（IO 边界） ------------------------------------------------

    def _ensure_conn(self) -> sqlite3.Connection:
        """在 executor 线程上惰性建连接并建表。单 worker 保证这里无竞争。"""
        if self._conn is None:
            conn = sqlite3.connect(self._path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                # 内存库与部分文件系统不支持 WAL；降级到默认日志模式即可。
                pass
            apply_schema(conn)
            self._conn = conn
        return self._conn

    async def _run(self, fn: Callable[[sqlite3.Connection], _T]) -> _T:
        if self._closed:
            raise RuntimeError("SQLiteStateStore 已关闭")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(self._ensure_conn()))

    async def close(self) -> None:
        """关闭连接并回收线程。重复调用是 no-op。"""
        if self._closed:
            return

        def _close(conn: sqlite3.Connection) -> None:
            conn.close()

        await self._run(_close)
        self._conn = None
        self._closed = True
        self._executor.shutdown(wait=False)

    # ---- 绑定 -----------------------------------------------------------------

    async def get_binding(self, session_id: str) -> SandboxHandle | None:
        def _get(conn: sqlite3.Connection) -> SandboxHandle | None:
            row = conn.execute(
                "SELECT sandbox_id, sandbox_backend FROM roost_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return decode_binding(row)

        return await self._run(_get)

    async def bind(
        self, session_id: str, sandbox: SandboxHandle, stamp: RuntimeStamp
    ) -> None:
        """无条件绑定（upsert）。需要 CAS 语义时用 swap_binding。"""

        def _bind(conn: sqlite3.Connection) -> None:
            conn.execute(
                f"INSERT INTO roost_sessions ({_SESSION_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "    sandbox_id = excluded.sandbox_id, "
                "    sandbox_backend = excluded.sandbox_backend, "
                "    stamp_bound_at = excluded.stamp_bound_at, "
                "    stamp_template_id = excluded.stamp_template_id, "
                "    stamp_runtime_files_hash = excluded.stamp_runtime_files_hash, "
                "    updated_at = excluded.updated_at",
                self._session_values(session_id, sandbox, stamp),
            )

        await self._run(_bind)

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

        def _swap(conn: sqlite3.Connection) -> bool:
            values = self._session_values(session_id, new, stamp)
            if old is None:
                inserted = conn.execute(
                    f"INSERT INTO roost_sessions ({_SESSION_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(session_id) DO NOTHING",
                    values,
                )
                if inserted.rowcount == 1:
                    return True
                updated = conn.execute(
                    f"UPDATE roost_sessions SET {_SESSION_ASSIGNMENTS} "
                    "WHERE session_id = ? AND sandbox_id IS NULL",
                    (*values[1:], session_id),
                )
                return updated.rowcount == 1

            updated = conn.execute(
                f"UPDATE roost_sessions SET {_SESSION_ASSIGNMENTS} "
                "WHERE session_id = ? AND sandbox_id = ? AND sandbox_backend = ?",
                (*values[1:], session_id, old.sandbox_id, old.backend),
            )
            return updated.rowcount == 1

        return await self._run(_swap)

    async def get_stamp(self, session_id: str) -> RuntimeStamp | None:
        def _get(conn: sqlite3.Connection) -> RuntimeStamp | None:
            row = conn.execute(
                "SELECT stamp_bound_at, stamp_template_id, stamp_runtime_files_hash "
                "FROM roost_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return decode_stamp(row)

        return await self._run(_get)

    @staticmethod
    def _session_values(
        session_id: str, sandbox: SandboxHandle, stamp: RuntimeStamp
    ) -> tuple[Any, ...]:
        return (
            session_id,
            sandbox.sandbox_id,
            sandbox.backend,
            to_iso(stamp.bound_at),
            stamp.template_id,
            stamp.runtime_files_hash,
            utc_now_iso(),
        )

    # ---- session 临界区 -------------------------------------------------------

    @asynccontextmanager
    async def session_critical(self, session_id: str) -> AsyncIterator[None]:
        """session 级互斥临界区（附录 L）：包住"串行门检查 + begin_turn"。

        SQLite 实现 = 进程内 per-session `asyncio.Lock`，与本类的**单写者进程假设**
        一致：跨进程/跨实例的互斥不在 SQLite 默认实现的承诺范围内，需要多消费者
        请用 PostgresStateStore（advisory lock 跨连接生效）。

        持有时间 = 临界区本身（毫秒级）。调用方绝不可把 runner 执行期放进来。
        """
        lock, waiters = self._session_locks.get(session_id, (None, 0))
        if lock is None:
            lock = asyncio.Lock()
        self._session_locks[session_id] = (lock, waiters + 1)
        try:
            async with lock:
                yield
        finally:
            _, count = self._session_locks[session_id]
            if count <= 1:
                del self._session_locks[session_id]
            else:
                self._session_locks[session_id] = (lock, count - 1)

    # ---- turn 生命周期 --------------------------------------------------------

    async def has_active_turn(
        self, session_id: str, *, exclude_turn_id: str | None = None
    ) -> bool:
        """session 是否有活跃 turn（session 串行门）。

        锁过期的 running 行**不算** active：wedged turn 的接管只走
        sweep → requeue → 重投递路径，不在这里被当成占位者。
        """

        def _has(conn: sqlite3.Connection) -> bool:
            sql = (
                "SELECT 1 FROM roost_turns "
                "WHERE session_id = ? AND status = ? AND locked_until > ?"
            )
            params: list[Any] = [session_id, STATUS_RUNNING, self._now()]
            if exclude_turn_id is not None:
                sql += " AND turn_id <> ?"
                params.append(exclude_turn_id)
            return conn.execute(sql + " LIMIT 1", params).fetchone() is not None

        return await self._run(_has)

    async def begin_turn(self, turn: TurnEnvelope, *, lock_seconds: int) -> bool:
        """CAS 开始一个 turn（sender 侧幂等门）。

        返回 True 当且仅当：行不存在（插入 running），或行 status='requeued'
        （接管为 running 并 bump attempt）。行为 running（**无论锁是否过期**）
        或 finished/failed 一律 False——begin_turn 自身绝不抢锁。
        """

        def _begin(conn: sqlite3.Connection) -> bool:
            locked_until = self._now() + lock_seconds
            payload = dumps_json(turn.payload)
            context = dumps_json(turn.context)
            inserted = conn.execute(
                f"INSERT INTO roost_turns ({_TURN_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(turn_id) DO NOTHING",
                (
                    turn.turn_id,
                    turn.session_id,
                    STATUS_RUNNING,
                    payload,
                    context,
                    turn.attempt,
                    locked_until,
                    utc_now_iso(),
                ),
            )
            if inserted.rowcount == 1:
                return True

            # 只有 requeued 行可被接管。attempt 的唯一 +1 所有者是投递方（重投时递增），
            # 这里只取 MAX(行值, envelope 值) 保持单调，绝不自行做加法——否则同一次
            # 重投会被行和 envelope 各记一次。
            taken = conn.execute(
                "UPDATE roost_turns SET "
                "    status = ?, attempt = MAX(attempt, ?), locked_until = ?, "
                "    payload = ?, context = ?, finished_at = NULL "
                "WHERE turn_id = ? AND status = ?",
                (
                    STATUS_RUNNING,
                    turn.attempt,
                    locked_until,
                    payload,
                    context,
                    turn.turn_id,
                    STATUS_REQUEUED,
                ),
            )
            return taken.rowcount == 1

        return await self._run(_begin)

    async def renew_turn_lock(self, turn_id: str, *, lock_seconds: int) -> None:
        """心跳续锁。仅作用于 status='running' 的行，其余静默 no-op。"""

        def _renew(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE roost_turns SET locked_until = ? WHERE turn_id = ? AND status = ?",
                (self._now() + lock_seconds, turn_id, STATUS_RUNNING),
            )

        await self._run(_renew)

    async def finish_turn(self, turn_id: str, *, status: str) -> None:
        """收尾。仅作用于 status='running' 的行，其余静默 no-op。

        status 只接受终态词汇 'finished' / 'failed' / 'attention'（后者是附录 M
        对附录 A 词表的修订：wall-clock 触顶 = 需要人工介入，不是失败）——
        'running' / 'requeued' 归 begin_turn / sweep 所有，放行会撞库内状态机。
        """
        if status not in TERMINAL_STATUSES:
            raise ValueError(
                f"finish_turn 只接受 {'/'.join(map(repr, TERMINAL_STATUSES))}，"
                f"得到 {status!r}"
            )

        def _finish(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE roost_turns SET status = ?, finished_at = ? "
                "WHERE turn_id = ? AND status = ?",
                (status, utc_now_iso(), turn_id, STATUS_RUNNING),
            )

        await self._run(_finish)

    async def bump_error_ordinal(self, turn_id: str) -> int:
        """driver error ordinal 自增，返回**自增后**的值（附录 M 的升级阶梯）。

        与 status 无关：ordinal 记的是"这个 turn 在沙箱里出了几次事"，跨 requeue、
        跨沙箱累积，因此 begin_turn 的接管刻意不复位它（内存 marker 会被重投清掉，
        那样阶梯永远停在第一级，同一个沙箱被反复 restart）。

        行不存在时返回 0（没有可自增的东西）。UPDATE + SELECT 放在同一事务里：
        单写者线程本已串行，事务是为了"读到的就是我刚写的那一次"这件事有据可依，
        而不依赖执行顺序的巧合。
        """

        def _bump(conn: sqlite3.Connection) -> int:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE roost_turns SET error_ordinal = error_ordinal + 1 "
                    "WHERE turn_id = ?",
                    (turn_id,),
                )
                row = conn.execute(
                    "SELECT error_ordinal FROM roost_turns WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return 0 if row is None else int(row["error_ordinal"])

        return await self._run(_bump)

    async def sweep_due_turns(self, *, limit: int) -> list[TurnEnvelope]:
        """到期未完成的 turn，转/保持 requeued 并返回，供 watchdog 重投。

        选出与转状态在**单事务**内完成，否则并发 watchdog 会重复认领同一批。
        返回的 attempt 是行内原值；+1 由投递方重投时负责。

        谓词是 `status IN ('running','requeued') AND locked_until <= now`：
        标记 requeued 时同步把 locked_until 推到 now + redelivery_grace——它在
        requeued 状态下的含义是**重投期限**。若 sweep 之后 enqueue 失败或宿主
        崩溃（sweep 与重投之间没有原子性），该行会在期限到期后被再次扫出，
        搁浅自愈；重复重投由 begin_turn 的接管语义吸收（附录 H 增补）。
        """

        def _sweep(conn: sqlite3.Connection) -> list[TurnEnvelope]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    f"SELECT {_TURN_COLUMNS} FROM roost_turns "
                    "WHERE status IN (?, ?) AND locked_until <= ? "
                    "ORDER BY locked_until LIMIT ?",
                    (STATUS_RUNNING, STATUS_REQUEUED, self._now(), limit),
                ).fetchall()
                if rows:
                    turn_ids = [row["turn_id"] for row in rows]
                    placeholders = ", ".join("?" for _ in turn_ids)
                    conn.execute(
                        f"UPDATE roost_turns SET status = ?, locked_until = ? "
                        f"WHERE turn_id IN ({placeholders})",
                        (
                            STATUS_REQUEUED,
                            self._now() + self._redelivery_grace_seconds,
                            *turn_ids,
                        ),
                    )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return [decode_turn(row) for row in rows]

        return await self._run(_sweep)
