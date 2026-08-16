"""宿主 ports（全部 typing.Protocol）。

契约见 CONTRACTS.md《宿主 ports》一节；逐字展开，不增删方法、不提供默认实现。
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from .events import DisplayEvent
from .types import RuntimeStamp, SandboxHandle, SessionBootContext, TurnEnvelope

__all__ = [
    "TurnDelivery",
    "StateStore",
    "SnapshotStore",
    "SnapshotKeyFn",
    "SandboxBackend",
    "EventSink",
    "SessionContextProvider",
    "OpsRecorder",
]


class TurnDelivery(Protocol):
    async def enqueue(self, turn: TurnEnvelope, *, not_before: datetime | None = None) -> None:
        """at-least-once 投递；重复投递由库的幂等机制吸收，投递层不承诺去重。"""


class StateStore(Protocol):
    """source of truth。表结构 deferred（DESIGN.md §6），方法面先钉死。"""

    async def get_binding(self, session_id: str) -> SandboxHandle | None: ...

    async def bind(self, session_id: str, sandbox: SandboxHandle, stamp: RuntimeStamp) -> None: ...

    async def swap_binding(self, session_id: str, old: SandboxHandle | None,
                           new: SandboxHandle, stamp: RuntimeStamp) -> bool:
        """CAS：仅当当前绑定 == old 时换绑到 new；forced update 的原子换绑入口。"""

    async def get_stamp(self, session_id: str) -> RuntimeStamp | None: ...

    async def has_active_turn(self, session_id: str, *, exclude_turn_id: str | None = None) -> bool: ...

    async def begin_turn(self, turn: TurnEnvelope, *, lock_seconds: int) -> bool:
        """CAS 开始一个 turn；已存在同 turn_id 的活跃行时返回 False（sender 侧幂等）。"""

    async def renew_turn_lock(self, turn_id: str, *, lock_seconds: int) -> None: ...

    async def finish_turn(self, turn_id: str, *, status: str) -> None:
        """status 与 Terminal.status 是不同值空间（turn 行状态后续含 requeued/expired 等），
        故意不共享枚举；Literal 化留到行为落地时。"""

    async def sweep_due_turns(self, *, limit: int) -> list[TurnEnvelope]:
        """锁过期且未完成的 turn，供 watchdog requeue。"""


class SnapshotStore(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes | None: ...


# snapshot key 派生由宿主提供，注入库配置：
SnapshotKeyFn = Callable[[str], str]   # session_id -> opaque key


class SandboxBackend(Protocol):
    async def create(self, *, template: str | None = None) -> SandboxHandle: ...

    async def connect(self, sandbox_id: str) -> SandboxHandle:
        """连接既有沙箱；实例处于暂停态时隐含恢复（不设独立 resume 方法）。"""

    async def pause(self, handle: SandboxHandle) -> None: ...

    async def kill(self, handle: SandboxHandle) -> None: ...

    async def upload(self, handle: SandboxHandle, files: dict[str, bytes]) -> None: ...

    async def exec(self, handle: SandboxHandle, argv: list[str], *,
                   env: dict[str, str] | None = None, timeout_seconds: float | None = None) -> tuple[int, str, str]: ...

    async def request(self, handle: SandboxHandle, method: str, path: str, *,
                      body: bytes | None = None, headers: dict[str, str] | None = None,
                      timeout_seconds: float | None = None) -> tuple[int, bytes]:
        """到沙箱内 driver loopback control server 的 HTTP 通道。"""


class EventSink(Protocol):
    async def emit(self, events: list[DisplayEvent]) -> None:
        """接收 reducer 输出；渲染与投递语义归宿主。库保证同一 turn 内 seq 递增。"""


class SessionContextProvider(Protocol):
    async def cold_boot_context(self, session_id: str) -> SessionBootContext:
        """cold boot 时库向宿主索取注入物；库不解释内容。"""


class OpsRecorder(Protocol):
    def record(self, event_type: str, /, **details: Any) -> None:
        """fire-and-forget：绝不 raise、绝不 await、可丢弃。同步签名是有意的。"""
