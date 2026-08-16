"""roost — durable, exactly-once agent sessions on disposable sandboxes.

骨架阶段：仅公开 CONTRACTS.md 钉死的类型、事件、ports 与协议常量，无行为实现。
"""

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
from .protocol import (
    ENV_PREFIX,
    HEADER_PREFIX,
    HEADER_PROTOCOL_VERSION,
    HEALTH_ENDPOINT,
    PROTOCOL_VERSION,
    TURN_ENDPOINT,
    UPDATE_ENDPOINT,
)
from .types import (
    RuntimeStamp,
    SandboxHandle,
    SessionBootContext,
    TurnEnvelope,
)

__version__ = "0.0.1"

__all__ = [
    "__version__",
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
    # protocol
    "PROTOCOL_VERSION",
    "HEADER_PREFIX",
    "HEADER_PROTOCOL_VERSION",
    "ENV_PREFIX",
    "TURN_ENDPOINT",
    "HEALTH_ENDPOINT",
    "UPDATE_ENDPOINT",
]
