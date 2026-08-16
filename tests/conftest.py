"""测试夹具。

`state_store` 夹具在「实现工厂表」上参数化：契约套件因此对任意 StateStore 实现复用，
未来的 Postgres 实现只需往 STATE_STORE_FACTORIES 里加一项，全部契约用例自动覆盖它。
"""

from __future__ import annotations

from collections.abc import Callable
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


# session_id -> StateStore 实现工厂。新增实现只需在此登记。
STATE_STORE_FACTORIES: dict[str, Callable[[FakeClock], Any]] = {
    "sqlite-memory": lambda clock: SQLiteStateStore(None, now=clock),
}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(params=list(STATE_STORE_FACTORIES), ids=list(STATE_STORE_FACTORIES))
async def state_store(request: pytest.FixtureRequest, clock: FakeClock):
    store = STATE_STORE_FACTORIES[request.param](clock)
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
