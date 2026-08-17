"""控制协议常量（骨架阶段仅常量）。

契约见 CONTRACTS.md《协议常量》一节。
"""

__all__ = [
    "PROTOCOL_VERSION",
    "HEADER_PREFIX",
    "HEADER_PROTOCOL_VERSION",
    "ENV_PREFIX",
    "TURN_ENDPOINT",
    "HEALTH_ENDPOINT",
    "UPDATE_ENDPOINT",
    "DEFAULT_CONTROL_PORT",
]

PROTOCOL_VERSION = "1"
HEADER_PREFIX = "X-Roost-"
HEADER_PROTOCOL_VERSION = "X-Roost-Protocol-Version"
ENV_PREFIX = "ROOST_"
TURN_ENDPOINT = "/v1/turn"
HEALTH_ENDPOINT = "/v1/health"
UPDATE_ENDPOINT = "/v1/update"

# driver loopback control server 的默认端口。属于协议面而非某一实现：沙箱内 driver
# 绑它、backend 发布它、宿主经它对话——三处必须是同一个数（附录 F 的常量统一，
# 关闭附录 D 遗留的 "DEFAULT_CONTROL_PORT 双处常量" 项）。
DEFAULT_CONTROL_PORT = 8787
