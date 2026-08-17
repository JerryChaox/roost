"""roost — durable, exactly-once agent sessions on disposable sandboxes.

公开 CONTRACTS.md 钉死的类型、事件、ports 与协议常量，以及 M1 状态与投递内核
（SQLite StateStore、进程内 TurnDelivery、turn pipeline），以及 M2 控制协议的
host 侧（wire 编解码与 ControlClient）。

driver 子系统（`roost.driver`）是沙箱内实现，刻意不在顶层导出——宿主只经
`roost.control` 与它对话。

`E2BSandboxBackend`（extra `roost[e2b]`）与 `PostgresStateStore`
（extra `roost[postgres]`，asyncpg）是**惰性导出**（PEP 562 的模块级
`__getattr__`）：核心必须零运行时依赖——没装 extra 的环境 `import roost` 照常成功，
只有真去取这些名字时才会 import 到可选依赖（没装则报安装指引）。
"""

from typing import TYPE_CHECKING, Any

from .control import (
    ControlClient,
    ControlError,
    ControlTimeoutError,
    EventPage,
    HealthStatus,
    ProtocolError,
    ProtocolVersionError,
    TurnSubmission,
    UnknownTurnError,
    decode_event,
    decode_turn,
    encode_event,
    encode_turn,
)
from .backends import DockerSandboxBackend
from .backup import BackupCoordinator
from .delivery import InProcessTurnDelivery
from .events import (
    Delta,
    DisplayEvent,
    DriverEvent,
    LifecycleNotice,
    Terminal,
    ToolEvent,
)
from .ports import (
    EventSink,
    OpsRecorder,
    SandboxBackend,
    SessionContextProvider,
    SnapshotKeyFn,
    SnapshotStore,
    StateStore,
    TurnDelivery,
)
from .fingerprint import runtime_fingerprint
from .install import DriverInstaller
from .pipeline import TurnProcessor
from .protocol import (
    DEFAULT_CONTROL_PORT,
    ENV_PREFIX,
    HEADER_PREFIX,
    HEADER_PROTOCOL_VERSION,
    HEALTH_ENDPOINT,
    PROTOCOL_VERSION,
    TURN_ENDPOINT,
    UPDATE_ENDPOINT,
    WORKSPACE_ENDPOINT,
)
from .reducer import (
    KIND_LIFECYCLE_NOTICE,
    KIND_TERMINAL,
    KIND_TEXT,
    KIND_TOOL,
    LIFECYCLE_SEQ_RESERVED,
    driver_display_seq,
    reduce_event,
    reduce_events,
)
from .runner import (
    DEFAULT_BOOT_GRACE,
    DEFAULT_FIRST_RENDERABLE,
    DEFAULT_LIVENESS_QUIET,
    DEFAULT_LONG_WATCH_INTERVAL,
    DEFAULT_PROGRESS_QUIET,
    DEFAULT_ROUND_BUDGET,
    DEFAULT_WALL_CLOCK_CEILING,
    ORDINAL_GIVE_UP,
    ORDINAL_KILL,
    ORDINAL_RESTART,
    SandboxTurnRunner,
    TurnAbandonedError,
    TurnFailedError,
    TurnNeedsAttentionError,
    TurnStalledError,
)
from .protocol import WORKSPACE_CONTENT_TYPE
from .sessions import (
    DEFAULT_UPDATE_BACKOFF,
    BindingConflictError,
    BootError,
    BootTimeoutError,
    SessionSandboxRegistry,
)
from .snapshot import FileSnapshotStore, S3Error, S3SnapshotStore
from .store import SQLiteStateStore
from .watchdog import Watchdog
from .types import (
    RuntimeStamp,
    SandboxHandle,
    SessionBootContext,
    TurnEnvelope,
)

if TYPE_CHECKING:  # 类型检查器要看得见真实符号，运行期仍然惰性
    from .backends import E2BSandboxBackend
    from .store import PostgresStateStore

__version__ = "0.0.1"

# 惰性导出名 -> 承载它的子模块（子模块自身也做惰性 import，见各自 __init__）。
_LAZY_EXPORTS = {
    "E2BSandboxBackend": "backends",   # extra roost[e2b]
    "PostgresStateStore": "store",     # extra roost[postgres]
}


def __getattr__(name: str) -> Any:
    """惰性解析可选依赖的导出（M7 的 E2BSandboxBackend、M11 的 PostgresStateStore）。"""
    submodule = _LAZY_EXPORTS.get(name)
    if submodule is not None:
        from importlib import import_module

        return getattr(import_module(f".{submodule}", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "__version__",
    # backends (M3a) & snapshot stores (M4)
    "DockerSandboxBackend",
    # backends (M7，可选依赖 roost[e2b]；惰性导出)
    "E2BSandboxBackend",
    "FileSnapshotStore",
    "S3SnapshotStore",
    "S3Error",
    # types
    "TurnEnvelope",
    "SandboxHandle",
    "SessionBootContext",
    "RuntimeStamp",
    # events
    "Delta",
    "ToolEvent",
    "LifecycleNotice",
    "Terminal",
    "DriverEvent",
    "DisplayEvent",
    # ports
    "TurnDelivery",
    "StateStore",
    "SnapshotStore",
    "SnapshotKeyFn",
    "SandboxBackend",
    "EventSink",
    "SessionContextProvider",
    "OpsRecorder",
    # M1 内核
    "SQLiteStateStore",
    # M11 多消费者（可选依赖 roost[postgres]；惰性导出）
    "PostgresStateStore",
    "InProcessTurnDelivery",
    "TurnProcessor",
    # M2 控制协议（host 侧；driver 内部名不进顶层导出）
    "encode_turn",
    "decode_turn",
    "encode_event",
    "decode_event",
    "ProtocolError",
    "ControlClient",
    "ControlError",
    "ControlTimeoutError",
    "ProtocolVersionError",
    "UnknownTurnError",
    "TurnSubmission",
    "EventPage",
    "HealthStatus",
    # M4 持久化整合（工作区备份/恢复）
    "BackupCoordinator",
    # M3b 编排（cold boot / reducer / turn runner）
    "DriverInstaller",
    "SessionSandboxRegistry",
    "BootError",
    "BootTimeoutError",
    "BindingConflictError",
    "SandboxTurnRunner",
    "TurnFailedError",
    # M5 watchdog 与 liveness
    "TurnStalledError",
    "Watchdog",
    # M12 生产级 watchdog 语义（双时钟 / 决策矩阵 / 升级阶梯）
    "TurnAbandonedError",
    "TurnNeedsAttentionError",
    "DEFAULT_BOOT_GRACE",
    "DEFAULT_LIVENESS_QUIET",
    "DEFAULT_PROGRESS_QUIET",
    "DEFAULT_FIRST_RENDERABLE",
    "DEFAULT_ROUND_BUDGET",
    "DEFAULT_LONG_WATCH_INTERVAL",
    "DEFAULT_WALL_CLOCK_CEILING",
    "ORDINAL_RESTART",
    "ORDINAL_KILL",
    "ORDINAL_GIVE_UP",
    # M6 零停机 forced update
    "runtime_fingerprint",
    "DEFAULT_UPDATE_BACKOFF",
    "reduce_event",
    "reduce_events",
    "driver_display_seq",
    "KIND_TEXT",
    "KIND_TOOL",
    "KIND_LIFECYCLE_NOTICE",
    "KIND_TERMINAL",
    "LIFECYCLE_SEQ_RESERVED",
    # protocol
    "PROTOCOL_VERSION",
    "HEADER_PREFIX",
    "HEADER_PROTOCOL_VERSION",
    "ENV_PREFIX",
    "TURN_ENDPOINT",
    "HEALTH_ENDPOINT",
    "UPDATE_ENDPOINT",
    "WORKSPACE_ENDPOINT",
    "WORKSPACE_CONTENT_TYPE",
    "DEFAULT_CONTROL_PORT",
]
