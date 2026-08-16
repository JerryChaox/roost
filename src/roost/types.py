"""核心类型。

契约见 CONTRACTS.md《核心类型》一节；本模块逐字展开该节，不额外引入抽象。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

__all__ = [
    "TurnEnvelope",
    "SandboxHandle",
    "SessionBootContext",
    "RuntimeStamp",
]


@dataclass(frozen=True)
class TurnEnvelope:
    turn_id: str                      # 确定性派生，幂等主键
    session_id: str
    payload: dict[str, Any]           # prompt / 消息批；库不解释结构
    context: dict[str, Any] = field(default_factory=dict)  # opaque 宿主 blob
    attempt: int = 1                  # 投递尝试计数，观测用，不参与幂等


@dataclass(frozen=True)
class SandboxHandle:
    sandbox_id: str
    backend: str                      # backend 标识，如 "e2b" / "docker"


@dataclass(frozen=True)
class SessionBootContext:
    files: dict[str, bytes] = field(default_factory=dict)   # 沙箱内路径 -> 内容
    env: dict[str, str] = field(default_factory=dict)
    skills: dict[str, bytes] = field(default_factory=dict)  # skill 路径 -> 内容


@dataclass(frozen=True)
class RuntimeStamp:
    bound_at: datetime
    template_id: str | None
    runtime_files_hash: str | None    # None = 快照构建期烘焙，首次正常重启前豁免比对
