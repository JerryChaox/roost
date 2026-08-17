"""测试夹具。

`state_store` 夹具在「实现工厂表」上参数化：契约套件因此对任意 StateStore 实现复用。
现有两项——SQLite 内存库与 Postgres（M11）。Postgres 实例用 docker 容器现起，容器带
`roost.sandbox=1` label 沿用清理纪律，夹具兜底 `docker rm -f`；本机无 docker 或未装
`roost[postgres]` 时该参数化 skip（附录 L）。
"""

from __future__ import annotations

import importlib.util
import subprocess
import time as _time
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from roost import RuntimeStamp, SandboxHandle, SQLiteStateStore, TurnEnvelope


class FakeClock:
    """可手动推进的 unix epoch 时钟。锁过期靠推进它，而不是靠真实 sleep。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


PG_IMAGE = "postgres:16-alpine"
PG_LABEL = "roost.sandbox=1"
PG_PASSWORD = "roost"
PG_DB = "roost_test"
PG_READY_TIMEOUT = 60.0


def _docker_available() -> bool:
    try:
        done = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _asyncpg_available() -> bool:
    return importlib.util.find_spec("asyncpg") is not None


@pytest.fixture(scope="session")
def postgres_dsn() -> Any:
    """现起一个 PG 容器供整个测试会话复用；无 docker / 无 asyncpg 则 skip。"""
    if not _asyncpg_available():
        pytest.skip("asyncpg 未安装（extra roost[postgres]）")
    if not _docker_available():
        pytest.skip("docker daemon unavailable")

    name = f"roost-pg-{uuid.uuid4().hex[:8]}"
    started = subprocess.run(
        [
            "docker", "run", "-d", "--name", name,
            "--label", PG_LABEL,
            "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}",
            "-e", f"POSTGRES_DB={PG_DB}",
            "-p", "127.0.0.1::5432",
            PG_IMAGE,
            # 测试库不需要崩溃安全，关掉刷盘让建表/清表快一个数量级。
            "-c", "fsync=off", "-c", "full_page_writes=off",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if started.returncode != 0:
        pytest.skip(f"PG 容器启动失败：{started.stderr.strip()}")

    try:
        port = _published_port(name)
        _wait_until_ready(name)
        yield f"postgresql://postgres:{PG_PASSWORD}@127.0.0.1:{port}/{PG_DB}"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=120)


def _published_port(container: str) -> str:
    done = subprocess.run(
        ["docker", "port", container, "5432/tcp"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if done.returncode != 0 or not done.stdout.strip():
        pytest.skip(f"读不到 PG 容器映射端口：{done.stderr.strip()}")
    return done.stdout.strip().splitlines()[0].rsplit(":", 1)[1]


def _wait_until_ready(container: str) -> None:
    """等到 PG 真的在 **TCP** 上接受连接。

    刻意不用 `pg_isready`（unix socket）：postgres 镜像的 initdb 阶段会先起一个
    只监听 unix socket 的临时服务，pg_isready 那时就已经返回 ready，而宿主侧的
    TCP 连接还会被拒——按它判定会拿到间歇性的连接失败。这里用容器内经 TCP 的
    psql 探测，那才是宿主将要走的同一条路。
    """
    deadline = _time.monotonic() + PG_READY_TIMEOUT
    last = ""
    while _time.monotonic() < deadline:
        done = subprocess.run(
            [
                "docker", "exec", "-e", f"PGPASSWORD={PG_PASSWORD}", container,
                "psql", "-h", "127.0.0.1", "-U", "postgres", "-d", PG_DB,
                "-tAc", "SELECT 1",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if done.returncode == 0:
            return
        last = (done.stderr or done.stdout).strip()
        _time.sleep(0.3)
    pytest.skip(f"PG 容器在 {PG_READY_TIMEOUT}s 内未就绪：{last}")


async def reset_postgres(dsn: str) -> None:
    """清空 PG 侧状态（建表是 store 启动时幂等做的，这里只负责丢旧表）。"""
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DROP TABLE IF EXISTS roost_turns")
        await conn.execute("DROP TABLE IF EXISTS roost_sessions")
    finally:
        await conn.close()


async def make_postgres_store(dsn: str, clock: FakeClock) -> Any:
    """建一个 PostgresStateStore 并把表建好（DDL 竞争留给测试自己决定顺序）。"""
    from roost import PostgresStateStore

    store = PostgresStateStore(dsn, now=clock)
    # 第一次读操作会惰性建池并幂等建表；先跑一次，让后续并发路径不撞 DDL。
    await store.has_active_turn("warmup")
    return store


# 参与契约套件的 StateStore 实现名。新增实现在此登记，全部契约用例自动覆盖它。
STATE_STORE_IMPLEMENTATIONS = ("sqlite-memory", "postgres")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(params=STATE_STORE_IMPLEMENTATIONS, ids=STATE_STORE_IMPLEMENTATIONS)
async def state_store(request: pytest.FixtureRequest, clock: FakeClock):
    if request.param == "postgres":
        dsn = request.getfixturevalue("postgres_dsn")
        await reset_postgres(dsn)
        store = await make_postgres_store(dsn, clock)
    else:
        store = SQLiteStateStore(None, now=clock)
    try:
        yield store
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            await close()


def make_turn(
    turn_id: str = "turn-1",
    session_id: str = "session-1",
    *,
    attempt: int = 1,
    payload: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> TurnEnvelope:
    return TurnEnvelope(
        turn_id=turn_id,
        session_id=session_id,
        payload={"text": "hello"} if payload is None else payload,
        context={} if context is None else context,
        attempt=attempt,
    )


def make_stamp(
    *, template_id: str | None = "tpl-1", runtime_files_hash: str | None = "sha-1"
) -> RuntimeStamp:
    return RuntimeStamp(
        bound_at=datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc),
        template_id=template_id,
        runtime_files_hash=runtime_files_hash,
    )


def make_handle(sandbox_id: str = "sbx-1", backend: str = "docker") -> SandboxHandle:
    return SandboxHandle(sandbox_id=sandbox_id, backend=backend)
