"""roost — durable, exactly-once agent sessions on disposable sandboxes.

公开 CONTRACTS.md 钉死的类型、事件、ports 与协议常量，以及 M1 状态与投递内核
（SQLite StateStore、进程内 TurnDelivery、turn pipeline），以及 M2 控制协议的
host 侧（wire 编解码与 ControlClient）。

driver 子系统（`roost.driver`）是沙箱内实现，刻意不在顶层导出——宿主只经
`roost.control` 与它对话。
"""

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
from .runner import SandboxTurnRunner, TurnFailedError, TurnStreamTimeoutError
from .sessions import (
    BindingConflictError,
    BootError,
    BootTimeoutError,
    SessionSandboxRegistry,
)
from .snapshot import FileSnapshotStore, S3Error, S3SnapshotStore
from .store import SQLiteStateStore
from .types import (
    RuntimeStamp,
    SandboxHandle,
    SessionBootContext,
    TurnEnvelope,
)

__version__ = "0.0.1"

__all__ = [
    "__version__",
    # backends (M3a) & snapshot stores (M4)
    "DockerSandboxBackend",
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
    # M3b 编排（cold boot / reducer / turn runner）
    "DriverInstaller",
    "SessionSandboxRegistry",
    "BootError",
    "BootTimeoutError",
    "BindingConflictError",
    "SandboxTurnRunner",
    "TurnFailedError",
    "TurnStreamTimeoutError",
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
    "DEFAULT_CONTROL_PORT",
]
