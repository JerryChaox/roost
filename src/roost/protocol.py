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
    "WORKSPACE_ENDPOINT",
    "WORKSPACE_CONTENT_TYPE",
    "DEFAULT_CONTROL_PORT",
]

PROTOCOL_VERSION = "1"
HEADER_PREFIX = "X-Roost-"
HEADER_PROTOCOL_VERSION = "X-Roost-Protocol-Version"
ENV_PREFIX = "ROOST_"
TURN_ENDPOINT = "/v1/turn"
HEALTH_ENDPOINT = "/v1/health"
UPDATE_ENDPOINT = "/v1/update"
# 工作区归档端点（附录 G）。M4 期间它暂住 control/client.py（那一里程碑的改动面
# 不含本文件），M5 收尾把它收回端点常量组——双端（host 客户端与沙箱内 server）
# 从此引同一处定义，client.py 仅作向后兼容的再导出。
WORKSPACE_ENDPOINT = "/v1/workspace"
# 工作区归档的载荷类型：两端（driver server 的响应头、host 客户端的请求头）必须
# 是同一个字符串，因此和端点一起住在协议面（附录 H 增补的遗留项，M6 收回）。
WORKSPACE_CONTENT_TYPE = "application/gzip"

# driver loopback control server 的默认端口。属于协议面而非某一实现：沙箱内 driver
# 绑它、backend 发布它、宿主经它对话——三处必须是同一个数（附录 F 的常量统一，
# 关闭附录 D 遗留的 "DEFAULT_CONTROL_PORT 双处常量" 项）。
DEFAULT_CONTROL_PORT = 8787
