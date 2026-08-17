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

## 附录 A：M1 内核契约（2026-08-17 钉定）

### StateStore 最小表结构（SQLite / Postgres 中性）

```
roost_sessions
  session_id TEXT PK
  sandbox_id TEXT NULL           -- NULL = 未绑定
  sandbox_backend TEXT NULL
  stamp_bound_at TEXT NULL       -- ISO8601 UTC
  stamp_template_id TEXT NULL
  stamp_runtime_files_hash TEXT NULL
  updated_at TEXT NOT NULL

roost_turns
  turn_id TEXT PK
  session_id TEXT NOT NULL
  status TEXT NOT NULL           -- 'running' | 'finished' | 'failed' | 'requeued'
  payload TEXT NOT NULL          -- JSON
  context TEXT NOT NULL          -- JSON
  attempt INTEGER NOT NULL
  locked_until REAL NOT NULL     -- unix epoch 秒
  created_at TEXT NOT NULL
  finished_at TEXT NULL
```

### 语义钉死

- `begin_turn` 返回 True 当且仅当：行不存在（插入 running）、或行 status='requeued'
  （接管为 running）。行为 running（无论锁是否过期）或 finished/failed 一律
  False——过期锁的接管**只**走 sweep→requeue→再投递路径，begin_turn 自身绝不抢锁。
- **attempt 的唯一 +1 所有者是投递方**（失败/sweep 重投时递增）。begin_turn 接管
  requeued 行时写 `MAX(行值, envelope 值)` 保持单调，绝不自行加法——行与 envelope
  永不双计。attempt 仅观测，不参与幂等。
- `sweep_due_turns`：单事务内选出 status='running' 且 locked_until<=now 的行，
  置为 'requeued' 并返回其 TurnEnvelope（attempt 原值；投递方重投时 +1）。
- `has_active_turn`：EXISTS(session_id 匹配、status='running'、locked_until>now)，
  可排除指定 turn_id。锁过期的 running 行不算 active（与来源实现一致：wedged turn
  可被 sweep 恢复）。
- `renew_turn_lock` / `finish_turn`：仅作用于 status='running' 的行，其余静默 no-op。
  `finish_turn` 的 status 只接受终态词汇 `'finished'`/`'failed'`，其余 raise
  ValueError——`'running'`/`'requeued'` 归 begin_turn/sweep 所有，禁止外部写入。
- `swap_binding(old=None)` 覆盖两种"未绑定"：行不存在（插入）与行存在但
  sandbox_id IS NULL（条件 UPDATE）。后者在 M1 无产生路径（尚无 unbind），
  为 M6 forced-update/解绑预留，契约套件暂不覆盖属预期。
- TurnProcessor 的 cancel 语义：process 被 cancel 时不吞 CancelledError、不
  finish_turn——让锁自然过期交给 sweep 认领；runner 抛异常是终态
  （finish_turn('failed')），恢复只走 sweep 单一路径。
- InProcessTurnDelivery：消费失败重投 attempt+1，达到 max_attempts（默认 5）停止
  重投并落入可检查的 `dropped`——投递层的放弃必须可观测，不得静默。
- **M1 串行化边界**：串行门与幂等门是两次独立 CAS，同 session 两个不同 turn
  并发消费时可能同时越过串行门；单消费者（默认 concurrency=1）下不可达，
  多 worker 的 session 互斥由后续里程碑的 advisory lock 承接。
- 全部状态转换用单语句条件 UPDATE 表达 CAS；不依赖跨语句事务隔离级别。
- 时间：locked_until 用宿主时钟 unix epoch。SQLite 默认实现按单写者进程假设，
  文档注明；Postgres 实现（后续里程碑）无此假设。

### M1 交付模块与内部接口

- `roost/store/sqlite.py`：SQLiteStateStore（实现 StateStore port）。
- `roost/delivery/inproc.py`：InProcessTurnDelivery（实现 TurnDelivery；asyncio 队列，
  at-least-once：消费失败重投并 attempt+1，可注入人为重复投递用于测试）。
- `roost/pipeline.py`：TurnProcessor——投递消费端。内部构造
  `TurnProcessor(store, runner)`，`runner: Callable[[TurnEnvelope], Awaitable[None]]`
  （M1 用 fake，M3 起接 sandbox 链路）。process 流程：
  `has_active_turn(排除自身) → 排队等待（简单重投延后）；begin_turn False → 丢弃；
  True → runner → finish_turn('finished'/'failed')`。runner 期间由 TurnProcessor
  以 lock_seconds/2 周期 renew_turn_lock。
- 测试（防回归目标明确）：StateStore 契约套件（begin/sweep/active/CAS 语义，
  对未来所有实现复用）；pipeline 幂等测试——并发把同一 turn_id enqueue N 次，
  runner 恰好执行 1 次；sweep 恢复测试——runner 挂死（锁过期）→ sweep → 重投 →
  第二次执行成功且总执行次数为 2（requeue 是显式允许的重跑）。

## 附录 B：M2 driver 子系统与控制协议契约（2026-08-17 钉定）

### Wire 约定

- 编码：JSON / UTF-8。请求与响应均带 header `X-Roost-Protocol-Version: 1`；
  版本不识别时响应 400 并在 body 给 `{"error": "unsupported_protocol_version"}`。
- TurnEnvelope wire 形状 = types.py 字段原名 JSON。DriverEvent wire 形状 =
  dataclass 字段 + 判别字段 `"type"`（"delta" | "tool_event" | "lifecycle_notice"
  | "terminal"）。编解码器是双端共用的同一实现（`roost/control/envelope.py`；
  driver 打包时携带同一份）。

### 端点行为

- `POST /v1/turn`：body 为 TurnEnvelope。200 响应
  `{"turn_id", "state": "accepted" | "duplicate"}`——重复提交（registry 已见
  turn_id）返回 "duplicate" 与既有条目现态，**绝不重新入队**（I1 driver 侧）。
  body 非法 → 400。
- `GET /v1/health`：200 `{"ok": true, "protocol_version", "uptime_ms",
  "harness_ready": bool}`。
- `GET /v1/turn/{turn_id}/events?after=<seq>&wait_ms=<n>`：长轮询 pull。返回
  seq > after 的事件列表与 `next_after`；无新事件时最多等待 wait_ms（上限 30000，
  默认 10000）再返回空列表。未知 turn_id → 404。**事件流承载机制就此钉定为
  host 长轮询 pull**（DESIGN §6 悬置项关闭）：只依赖 SandboxBackend.request
  已有通道，零新增基础设施；host 侧 cursor（after）语义让重复读天然幂等。
- `POST /v1/update`：M2 仅保留协议位，响应 501 `{"error": "reserved_until_m6"}`。

### Turn registry 语义（I1 driver 侧）

- 进程内 dict，键 turn_id，生命周期与 driver 进程绑定（DESIGN §四已明文）。
- 条目状态机：`queued → running → done(status)`；duplicate POST 在任何状态下
  只读返回，绝不产生第二次执行。
- 执行序：单 harness worker，FIFO——driver 内天然串行，不存在并发 turn。
- 事件存储：per-turn 内存列表，seq 自 1 单调递增，Terminal 恒为最后一条。
  M2 不设内存上限（有界化随 M6 生产化处理，PROTOCOL.md 注明）。

### Harness 接口（driver 内部 port）

```python
class Harness(Protocol):
    async def run(self, turn: TurnEnvelope, emit: Callable[[DriverEvent], None]) -> None:
        """执行一个 turn，经 emit 产出事件；实现负责在结束前 emit Terminal。
        run 抛异常时由 worker 兜底 emit Terminal(status='error')。"""
```

- M2 交付 `EchoHarness`（回显 payload 为若干 Delta + Terminal，可注入延迟/异常，
  测试用）。真实 Claude Agent SDK harness 归 M3。

### M2 交付模块

- `src/roost/control/envelope.py`（wire 编解码，纯函数，无 IO）、
  `src/roost/control/client.py`（宿主侧协议客户端：经 SandboxBackend.request
  提交 turn / 拉事件 / health，含超时与 duplicate 处理；不做业务重试策略）。
- `src/roost/driver/`：`server.py`（HTTP 路由与编解码边界）、`registry.py`
  （turn registry 状态机，纯内存无 IO，独立可测）、`worker.py`（FIFO 执行循环
  与异常兜底）、`harness.py`（Harness protocol + EchoHarness）、`emit.py`
  （seq 分配与 per-turn 事件缓存）、`__main__.py`（`python -m roost.driver`
  启动，端口经 ROOST_DRIVER_PORT，默认 8787，绑定 127.0.0.1）。
- driver 约束：仅标准库（沙箱内零安装依赖）；HTTP 实现选型（http.server /
  asyncio 手写最小实现）由实现者定，但路由/编解码/状态机/执行循环四类职责
  不得混居一个模块（ROADMAP 反腐化原则）。
- `PROTOCOL.md`（仓库根）：以本附录为骨架成文，含幂等契约明文段（DESIGN §四）
  与版本化规则。
- 测试：registry 状态机单测（duplicate 各状态只读返回）；**子进程端到端协议
  测试**——以 `python -m roost.driver` 起真实 driver 进程 + EchoHarness，走
  localhost HTTP：重复 POST 同 turn_id 恰好执行一次、事件长轮询含 cursor 续读、
  harness 异常时 Terminal(status='error') 兜底、health 就绪。envelope 编解码
  round-trip 属性测试（对四种事件类型）。

### M2 明确不包含

真实 Claude Agent SDK harness（M3）、driver 打包成单 artifact 与 fingerprint
（M6 前置）、`/v1/update` 行为（M6）、事件缓存有界化（M6）。
