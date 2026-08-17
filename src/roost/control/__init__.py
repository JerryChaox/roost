"""宿主侧控制协议实现（wire 编解码 + 协议客户端）。

契约见 CONTRACTS.md《附录 B：M2 driver 子系统与控制协议契约》与仓库根 PROTOCOL.md。
本包只面向 host 侧；driver 打包进沙箱时复用其中的 `envelope` 模块（同一份编解码实现，
双端不得各写一份）。
"""

from .client import (
    ControlClient,
    ControlError,
    ControlTimeoutError,
    EventPage,
    HealthStatus,
    ProtocolVersionError,
    TurnSubmission,
    UnknownTurnError,
)
from .envelope import (
    EVENT_TYPES,
    ProtocolError,
    decode_event,
    decode_event_bytes,
    decode_turn,
    decode_turn_bytes,
    encode_event,
    encode_event_bytes,
    encode_turn,
    encode_turn_bytes,
)

__all__ = [
    # envelope
    "EVENT_TYPES",
    "ProtocolError",
    "encode_turn",
    "decode_turn",
    "encode_turn_bytes",
    "decode_turn_bytes",
    "encode_event",
    "decode_event",
    "encode_event_bytes",
    "decode_event_bytes",
    # client
    "ControlClient",
    "ControlError",
    "ControlTimeoutError",
    "ProtocolVersionError",
    "UnknownTurnError",
    "TurnSubmission",
    "EventPage",
    "HealthStatus",
]
