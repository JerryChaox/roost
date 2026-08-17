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

## 附录 C：M4 SnapshotStore 实现契约（2026-08-17 钉定，与 M2 并行）

- `src/roost/snapshot/fs.py`：`FileSnapshotStore(root)`。opaque key → 文件名用
  URL 百分号编码（safe=''），不解释 key 结构；写入原子（同目录 tmp + rename）；
  get 未命中返回 None。
- `src/roost/snapshot/sigv4.py`：AWS Signature V4 纯函数实现（stdlib
  hmac/hashlib），带 AWS 官方文档 known-answer 测试向量。**零运行时依赖是硬约束，
  不引 boto3/aiohttp。**
- `src/roost/snapshot/s3.py`：`S3SnapshotStore(bucket, *, endpoint_url, region,
  access_key, secret_key, prefix="")`。path-style URL；PUT/GET object 单请求
  （不做 multipart，对象大小上界由宿主负责，文档注明）；GET 404 → None，
  其余非 2xx raise。IO 用 urllib + 线程 executor 包装成 async。endpoint_url
  可覆盖 → 兼容 MinIO / GCS HMAC 互操作。
- 测试：FS roundtrip / 原子性 / 未命中；SigV4 known-answer 向量；stdlib 假 S3
  HTTP server 的 PUT/GET roundtrip + Authorization header 结构断言。
  不访问真实云。

## 附录 D：M3a DockerSandboxBackend 契约（2026-08-17 钉定，与 M2 并行）

- `src/roost/backends/docker.py`：实现 SandboxBackend port，**shell 出 docker CLI**
  （asyncio subprocess），不引 docker SDK。容器标 label `roost.sandbox=1`。
- `create(template=镜像名，默认 "python:3.12-slim")`：`docker run -d`，容器主进程
  `sleep infinity`，控制端口 8787（模块内常量 DEFAULT_CONTROL_PORT，与 driver
  默认一致；统一常量位置留 M3 集成时处理）发布到 127.0.0.1 的临时宿主端口。
- `connect(sandbox_id)`：docker inspect 验证存在；处于 paused 时 unpause（契约：
  connect 隐含恢复）。不存在 → raise。
- `pause` → docker pause；`kill` → docker rm -f。
- `exec` → docker exec（env 经 -e），timeout 到期终止并 raise。
- `upload`：files dict（容器内绝对路径 → bytes）打成内存 tar，经
  `docker cp - <id>:/` 落盘。
- `request`：docker port 解析 8787 的宿主映射端口，urllib 对 127.0.0.1 发 HTTP
  （线程 executor 包装）。
- 测试：本机无 docker 时整文件 skip；覆盖 create→exec 回显、upload 后 exec 读回、
  exec 起 `python -m http.server 8787`（detached）后 request 打通、pause→connect
  隐含恢复、fixture 兜底 rm -f 不留容器。
- 边界：不改 `src/roost/__init__.py`（导出在集成时统一加）、不碰 control/ 与
  driver/（M2 写入集）。附录 C 同样受此边界约束。

## 附录 E：集成裁定记录（2026-08-17，M2/M3a/M4 落地时）

- 附录 B 增补（M2 实现裁定采纳）：`POST /v1/turn` 响应含第三字段
  `turn_state`（"queued"|"running"|"done"）；缺失协议版本 header 视为不识别
  → 400；错误码表（invalid_body / invalid_query / unknown_turn / not_found /
  method_not_allowed）归 PROTOCOL.md §6 所有；worker 对"harness 正常返回但未
  emit Terminal"同样兜底 Terminal(status='error')——"Terminal 恒为最后一条"是
  driver 不变量；driver 增设 httpd.py 承载 HTTP wire（server.py 仅路由与编解码）。
- 附录 C 增补（M4 实现裁定采纳）：`prefix` 是字面前缀（目录语义自带 '/'）；
  `timeout_seconds=30.0` kw-only 参数；`region` 默认 "us-east-1"、`endpoint_url`
  默认 AWS 区域端点。`x-amz-security-token` 与 port 面的 delete/exists 明确
  deferred——等 backup coordinator 真需要时经契约扩。
- 附录 D 增补（M3a 实现裁定，均留 M3 集成处理）：exec 无 detach 参数，detached
  以 `sh -c "nohup … &"` 调用方约定表达；connect 对 exited 容器原样返回 handle，
  活性判定归 registry 的 health 探测；DEFAULT_CONTROL_PORT 双处常量待统一。

## 附录 F：M3b 编排与首个端到端契约（2026-08-17 钉定）

范围裁定：M3b = cold boot 编排 + 事件 reducer + turn runner 接线 + CLI demo，
harness 用 EchoHarness（Demo 1 验收 exactly-once，与 LLM 无关）；真实
Claude Agent SDK harness 拆为 M3c（M8 前完成）；快照恢复/备份不在本段（M4）。

### 交付模块

- `src/roost/install.py`：DriverInstaller——用 importlib 遍历收集已安装 roost 包
  源码（含 driver/control/events/types/protocol），映射为
  `/opt/roost/src/roost/**` 的 files dict；产出启动命令
  `sh -c "nohup env ROOST_DRIVER_PORT=8787 PYTHONPATH=/opt/roost/src
  python -m roost.driver >/tmp/roost-driver.log 2>&1 &"`（附录 D 的 detached 约定）。
- `src/roost/sessions.py`：SessionSandboxRegistry——
  `get_or_create(session_id) -> (SandboxHandle, ControlClient)`：
  有绑定 → backend.connect → health 探测（短超时）通过即复用；探测失败视为死
  沙箱 → cold boot 新沙箱并 `store.swap_binding(old, new)`；无绑定 → cold boot
  + `swap_binding(None, new)`（附录 A 预留分支自此可达）。cold boot 流程：
  create → upload(DriverInstaller.files) → exec 启动命令 → 轮询 /v1/health 就绪
  （超时默认 30s，超时 raise 并 kill 半成品沙箱，绝不留未绑定的活容器）。
  boot 期间经注入的 EventSink 发 lifecycle_notice（boot_started/boot_finished，
  含 elapsed_ms）。SessionContextProvider 的注入物并入同一次 upload。
- `src/roost/reducer.py`：纯函数 DriverEvent → DisplayEvent
  （delta→text、tool_event→tool、lifecycle_notice→lifecycle_notice、
  terminal→terminal；body 为源事件字段的 dict，session_id 由调用方补入）。
- `src/roost/runner.py`：SandboxTurnRunner——实现 M1 TurnProcessor 的 runner
  签名：get_or_create → ControlClient 提交 turn（duplicate 视为已在跑，照常
  拉流）→ 长轮询拉事件至 Terminal → 经 reducer 逐批送 EventSink。Terminal
  status='error' 时 runner raise（让 M1 pipeline 记 'failed'）。
- `examples/cli_chat.py`：REPL demo 宿主——逐行输入 → 确定性 turn_id
  （sha256(session_id + 行序号 + 文本)）→ delivery.enqueue；EventSink 打印
  text/lifecycle；`--duplicate` 让每条消息 enqueue 两次（Demo 1 演示入口）；
  `--backend docker` 默认。
- 常量统一（附录 D 遗留）：DEFAULT_CONTROL_PORT 收敛到 `roost/protocol.py`，
  backends 与 driver 引用之。

### 验收测试

- 编排端到端（需 docker，无则 skip）：SessionSandboxRegistry cold boot 真容器
  → runner 跑一个 EchoHarness turn → EventSink 收到 text…terminal 且 seq 递增；
  同 turn_id 经 delivery 双投 → runner 只执行一次、EventSink 只收到一份终态
  （Demo 1 的自动化形态）。
- 复用与死沙箱路径：连续两 turn 复用同一 sandbox_id；docker rm -f 后下一 turn
  自动 cold boot 新沙箱且 swap_binding 生效。
- reducer 纯函数单测。boot 超时路径：注入永不就绪的假 backend → raise 且 kill
  被调用。

## 附录 F 增补：M3b 落地裁定（2026-08-17）

- **driver 绑定地址**：附录 B"绑定 127.0.0.1"修订为——driver 默认绑定
  127.0.0.1，可经 `ROOST_DRIVER_HOST` 覆盖；容器类 backend 的端口发布只达容器
  网卡，故 DriverInstaller 默认传 `ROOST_DRIVER_HOST=0.0.0.0`，对外暴露仍由
  backend 的宿主回环端口发布约束（E2B 类 backend 传回 127.0.0.1）。附录 F 的
  启动命令字符串按此修订。
- **display seq 保留段**：display 流的 seq 1..16（`LIFECYCLE_SEQ_RESERVED`）
  保留给库产 lifecycle notice（boot_started=1、boot_finished=2，M6 update 用
  后续位），driver 事件经 `driver_display_seq` 偏移到保留段之后——无状态、
  重复 cursor 读幂等，"同一 turn 内 seq 递增"承诺保持；seq 有空洞属预期。
- 其余采纳：get_or_create 增 kw-only `turn_id=""`（notice 挂靠）；DriverInstaller
  整包收集 roost 源码（全 stdlib，不会烂）；`RuntimeStamp.runtime_files_hash`
  维持 None 至 M6 fingerprint；swap CAS 失败 kill 新沙箱并 raise。
