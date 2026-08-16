# roost — 接口契约（骨架实现的唯一依据）

状态：v1（2026-08-17）。本文件钉死 port 签名与核心类型；实现骨架时**逐字展开，
不得增删方法、不得添加本文件之外的抽象**。语义背景见 DESIGN.md。

约定：全部 async（`OpsRecorder.record` 除外）；`session_id` / `turn_id` /
`sandbox_id` / snapshot key 均为 opaque `str`；库内不出现任何宿主领域词汇。

## 核心类型（`roost/types.py`）

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

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
```

## 驱动器事件（`roost/events.py`）

driver → host 的 wire 事件与 reducer 输出。全部 frozen dataclass；
`DriverEvent = Delta | ToolEvent | LifecycleNotice | Terminal`（union 别名）。

```python
@dataclass(frozen=True)
class Delta:
    turn_id: str
    text: str
    seq: int

@dataclass(frozen=True)
class ToolEvent:
    turn_id: str
    name: str
    phase: str                        # "start" | "result"
    detail: dict[str, Any]
    seq: int

@dataclass(frozen=True)
class LifecycleNotice:
    turn_id: str
    kind: str                         # "boot_started" | "boot_finished" | "update_started" | "update_finished"
    elapsed_ms: int
    seq: int

@dataclass(frozen=True)
class Terminal:
    turn_id: str
    status: str                       # "ok" | "error"
    error: str | None
    usage: dict[str, Any]
    seq: int
```

reducer 输出 `DisplayEvent`（骨架阶段仅占位定义）：

```python
@dataclass(frozen=True)
class DisplayEvent:
    session_id: str
    turn_id: str
    kind: str                         # "text" | "tool" | "lifecycle_notice" | "terminal"
    body: dict[str, Any]
    seq: int
```

## 宿主 ports（`roost/ports.py`，`typing.Protocol` + 一个 `SnapshotKeyFn` 类型别名）

```python
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
```

## 协议常量（`roost/protocol.py`，骨架阶段仅常量）

```python
PROTOCOL_VERSION = "1"
HEADER_PREFIX = "X-Roost-"
HEADER_PROTOCOL_VERSION = "X-Roost-Protocol-Version"
ENV_PREFIX = "ROOST_"
TURN_ENDPOINT = "/v1/turn"
HEALTH_ENDPOINT = "/v1/health"
UPDATE_ENDPOINT = "/v1/update"
```

## 骨架阶段明确不包含

默认实现（进程内队列、SQLite store、E2B backend）、registry/watchdog/reducer 逻辑、
driver 本体、任何测试（无行为可保护）。这些属于后续里程碑。
