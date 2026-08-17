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

## 附录 G：M4 持久化整合契约（2026-08-17 钉定）

范围：workspace 备份/恢复 + Demo 2。存储层（附录 C）已落地，本段是接线。

### 协议 v1 扩展（PROTOCOL.md 同步更新）

- driver 启动时确保工作区目录存在：`ROOST_WORKSPACE_DIR`，默认 `/workspace`。
- `GET /v1/workspace`：返回工作区目录的 tar.gz 字节（Content-Type
  application/gzip）。空目录返回空归档。打包失败 → 500。
- `PUT /v1/workspace`：body 为 tar.gz，解包覆盖进工作区目录（成员路径必须
  相对且不得逃逸目录，违规 400）。解包失败 → 500。
- 设计理由记录：持久化走控制面复用 `SandboxBackend.request` 通道，
  SandboxBackend port 不为此扩 download 方法。

### 交付模块与接线

- `src/roost/driver/workspace.py`：tar.gz 打包/解包（纯逻辑 + 文件 IO，路径
  逃逸防护）；server.py 挂两个端点。
- `src/roost/control/client.py`：增 `get_workspace() -> bytes` 与
  `put_workspace(data: bytes) -> None`。
- `src/roost/backup.py`：BackupCoordinator——`schedule(session_id, client)`
  fire-and-forget：GET workspace → `SnapshotStore.put(snapshot_key(session_id))`。
  失败经 OpsRecorder 记录，**绝不影响 turn 结果**（I2）；同 session 并发去重
  （已在跑则跳过本次）。测试可等待的句柄（`drain()`）。
- `src/roost/runner.py`：Terminal（ok 或 error 均）后调度一次备份。
- `src/roost/sessions.py`：cold boot 在 health 就绪后、返回前：
  `SnapshotStore.get(snapshot_key)` 命中则 `put_workspace` 恢复；未命中直接返回
  （首次会话）。恢复失败按 boot 失败处理（kill 半成品）。registry 构造参数增
  `snapshot_store` + `snapshot_key`（均可选，缺任一则持久化整体禁用）。
- EchoHarness 增 counter 行为（payload 键 `counter: true` 时递增
  `$ROOST_WORKSPACE_DIR/counter` 并在回显附带值）——测试面，协议零增。
- `examples/cli_chat.py`：REPL 增 `/kill` 元命令（rm -f 当前沙箱，演示重生）。

### 验收测试

- workspace 打包/解包单测（含路径逃逸拒绝）。
- BackupCoordinator：成功写入 store；GET 失败不抛出、经 ops 记录；并发去重。
- Demo 2 端到端（真 docker）：counter turn（=1）→ 备份完成 → `docker rm -f`
  → 下一 turn 自动重建并恢复工作区 → counter=2。恢复失败路径：注入损坏
  snapshot → boot 失败且不留活容器。

## 附录 H：M5 watchdog 与 liveness 契约（2026-08-17 钉定）

### 卡死恢复的单一路径（I3 前半）

hang 的定义：turn 已提交、driver 事件流在 `stall_timeout` 内颗粒无收（以"含事件
的响应页"为准，空长轮询页不重置计时）。恢复只有一条路径：

1. `runner.py`：长轮询循环维护"上次收到事件"的时刻；停滞超过 `stall_timeout`
   → 经 registry 销毁当前沙箱（backend.kill + ops 记录 `sandbox_stalled_killed`；
   绑定行保留，指向死沙箱——下次 get_or_create 的 health 探测自然走 cold boot）
   → raise `TurnStalledError`（定义在 runner.py）。
2. `pipeline.py`：`except TurnStalledError` 专案——**不 finish_turn、不吞成
   failed**，heartbeat 随 process 返回而停，锁在 lock_seconds 内自然过期。
   这是附录 A"过期锁只走 sweep 路径"的消费端。
3. `src/roost/watchdog.py`：`Watchdog(store, delivery, *, interval, sweep_limit,
   ops=None)`——后台循环 `sweep_due_turns(limit)`，对每个返回的 envelope
   `enqueue(replace(attempt+1))`（附录 A：投递方是 attempt 唯一 +1 所有者）；
   非空 sweep 经 ops 记 `watchdog_requeued`（含 turn_id 列表）；start/stop
   生命周期与取消语义同既有后台任务模式。
4. 重投的 turn 经正常投递 → begin_turn 接管 requeued 行 → get_or_create 对死
   沙箱 cold boot（工作区自快照恢复，M4 已通）→ 新 driver 空 registry → 重跑
   合法（I1 的唯一合法重跑形态）。

### idle 不误杀（I3 的另一半）

- 停滞计时以事件为粒度：慢而活着的 harness（事件间隔 < stall_timeout）永不触发。
- `stall_timeout` 必须显著大于事件自然间隔、且与 lock_seconds 无耦合（锁由
  heartbeat 维持，只在 process 退出后才会过期）。默认 60s，kw-only 可调。

### 注入与测试

- EchoHarness 增 payload 键 `hang_on_attempt: N`——`turn.attempt == N` 时不产出
  任何事件并永久挂起（测试面，协议零增）。
- 验收测试：
  - 端到端（真 docker，短 stall_timeout/lock_seconds/interval）：
    `hang_on_attempt=1` 的 turn → 停滞被杀 → watchdog requeue → attempt=2 在新
    沙箱跑通拿到答案；断言 harness 恰好执行 2 次、行终态 finished、旧沙箱已销毁。
  - idle 不误杀：事件间隔逼近但不超 stall_timeout 的慢 turn 正常完成，sweep 全程
    空转、零 requeue。
  - watchdog 单测（fake store/delivery）：sweep 空 → 无动作；非空 → 逐个
    attempt+1 重投 + ops 记录；stop 干净取消。
- 收尾项：`WORKSPACE_ENDPOINT` 自 control/client.py 收回 protocol.py
  （附录 G 疑点 1 的裁定归宿）。

## 附录 H 增补：M5 落地裁定（2026-08-17）

- **删除 `TurnStreamTimeoutError`/`turn_timeout`**：wall-clock 总时长上限与
  "idle 不误杀"矛盾（会砍稳定出事件的长 turn），且其 failed 出口给卡死留了
  第二条含糊路径。卡死判据唯一（含事件页间隔 > stall_timeout），出口唯一
  （杀沙箱 + 不收尾 + sweep requeue）。
- **pipeline 对 TurnStalledError 正常返回而非外抛**：外抛会触发投递层消费失败
  重投，凭空多出第二条恢复路径。
- **sweep 搁浅自愈**（附录 A sweep 语义修订）：标记 requeued 时同步
  `locked_until = now + redelivery_grace`（SQLiteStateStore 构造参数，默认 30s；
  requeued 态下 locked_until 的含义是重投期限）；sweep 谓词改为
  `status IN ('running','requeued') AND locked_until <= now`。enqueue 失败或
  宿主在 sweep 与重投之间崩溃的行到期自动再被扫出；重复重投由 begin_turn
  接管语义吸收。port 签名零变化。
- 停滞路径不备份工作区（不向已判定不响应的 driver 发请求）；
  `watchdog_sweep_failed` / `watchdog_requeue_failed` ops 事件名采纳。
- 遗留：`WORKSPACE_CONTENT_TYPE` 仍在 control/client.py，随下次触碰该文件的
  里程碑一并收回 protocol.py。

## 附录 I：M6 零停机 forced update 契约（2026-08-17 钉定）

### Fingerprint

- `src/roost/fingerprint.py`：`runtime_fingerprint(installer) -> str`——对
  DriverInstaller 文件表（路径排序 + 内容）计算 sha256。这是"沙箱内运行时是否
  过期"的唯一判据。
- `sessions.py` cold boot 的 bind/swap 从此写入真实
  `RuntimeStamp(runtime_files_hash=fingerprint)`（终结 M3b 的 None 占位）。

### 触发与流程（get_or_create 内，复用路径命中健康沙箱时）

1. 触发条件：stamp.runtime_files_hash 非 None 且 ≠ 当前 fingerprint，且该
   session 不在 backoff 中。stamp 为 None（legacy/快照烘焙）不触发（M6 内不
   处理 legacy 迁移）。
2. 替换式更新（不走 /v1/update，端点维持 501 reserved，PROTOCOL.md 措辞同步）：
   旧沙箱是健康的、是状态的 source of truth——`get_workspace`（对旧 driver）
   取**活内存快照** → cold boot 新沙箱并以这份字节恢复（绕过 SnapshotStore，
   防 mis-keyed 快照空启动）→ health 就绪 → `swap_binding(old, new)` CAS
   原子换绑 → 换绑成功后才 kill 旧沙箱。
3. **失败永不伤 turn**（I3）：任一步失败（取快照失败 / 新沙箱 boot 失败 /
   CAS 失败）→ kill 新半成品、旧沙箱保持绑定并照常服务本 turn、写入
   per-session backoff（进程内 dict，默认 1800s，构造参数可调；跨进程 backoff
   属生产化范围，文档注明）。ops 记 `forced_update_aborted`（快照失败）/
   `forced_update_failed`（boot/CAS 失败）/ `forced_update_completed`。
4. lifecycle notice：`update_started`（保留段 seq=3）/ `update_finished`
   （seq=4），挂当前 turn 流；失败路径不发 update 事件（旧沙箱继续答题，
   状态无缝回落）。

### 验收测试

- fingerprint 单测：文件表内容/路径变化 → 变；顺序无关。
- e2e（真 docker）：turn1 counter=1 于沙箱 A → 用注入的 fingerprint 差异构造
  "新版本" registry → turn2：update notices 出现、执行于新沙箱 B（sandbox_id
  变化）、counter=2（状态经活快照延续）、A 已销毁、stamp 更新为新 fingerprint。
- 失败回退 e2e：注入新沙箱 boot 必败 → turn2 仍在 A 上正常答复（counter 照增）、
  A 仍绑定、backoff 生效（turn3 在 backoff 窗口内不再尝试更新，ops 无第二次
  forced_update_*）。
- swap CAS 失败路径单测（fake store）：新沙箱被 kill、不覆盖他人绑定。

## 附录 I 增补：M6 落地裁定（2026-08-17）

- **update notice 成对后置**：附录 I 的"started 作进度"与"失败路径不发事件"
  不可兼得；裁定换绑成功后成对补发 started(0)+finished(total)，失败路径零事件、
  显示流永不悬空"更新中"。真进度心跳留作宿主可选需求，暂不立项。
- get_stamp 的 store 读取失败照常抛出（与相邻 get_binding 同待遇）——
  "失败永不伤 turn"覆盖更新流程自身的三条失败路径，不覆盖 store 抖动。
- 换绑成功即 kill 旧沙箱，不加 has_active_turn 保护——与 M1 串行化边界一致
  （单消费者下无并发 turn；多消费者互斥属 advisory lock 里程碑）。
- `reserved_until_m6` 错误码字面保留（wire 兼容），语义为永久 reserved，
  PROTOCOL.md 已说明。
- backoff：进程内 monotonic dict，默认 1800s，0 关闭；跨进程 backoff 属
  生产化范围。
- fingerprint 采用长度前缀编码防拼接碰撞（{"ab":"c"} ≠ {"a":"bc"}）。

## 附录 J：M7 E2BSandboxBackend 契约（2026-08-17 钉定）

- **可选依赖**：核心零依赖不破——pyproject `[project.optional-dependencies]`
  增 `e2b = ["e2b>=<实现时核实的当前主版本>"]`；`src/roost/backends/e2b.py`
  内部惰性 import，未装 extra 时实例化报清晰错误（指导 `pip install roost[e2b]`）。
- **凭据**：构造参数 `api_key` 或环境变量 `ROOST_E2B_API_KEY`（内部映射给 SDK）。
- **port 映射**（以 E2B 官方 SDK 现行文档核实为准，不凭记忆）：
  create → 创建沙箱（template 参数 = E2B template id，None 用 E2B 默认）；
  connect → 连接既有沙箱，暂停实例隐含恢复（契约 docstring）；pause → E2B
  暂停能力；kill → 销毁；exec → 命令执行（env/timeout 语义对齐 Docker 版）；
  upload → 文件写入；request → 经 E2B 的沙箱端口 host URL 对
  DEFAULT_CONTROL_PORT 发 HTTPS。
- **driver 绑定地址**：E2B 端口代理可达哪个 interface 以实测/文档为准；结论
  写进模块 docstring 并回报（附录 F 增补的 bind_host 参数按结论传）。
- **测试**：`tests/test_e2b_backend.py` 无 `ROOST_E2B_API_KEY` 时整文件 skip；
  有 key 时镜像 Docker backend 套件形态（create/exec/upload/request/
  pause→connect/kill/清理不留沙箱）+ 一条编排冒烟（cold boot + 单 turn）。
  不依赖 key 的部分（惰性 import 报错、URL/参数组装）常规单测。
- **验收后置**：ROADMAP M7 的"三 demo 在 E2B 原样通过"在 key 提供后执行，
  在此之前 M7 todo 保持 open。

## 附录 J 增补：M7 落地裁定（2026-08-17）

- **附录 G 修订：工作区默认值 `/workspace` → `~/workspace`**（driver 启动
  expanduser；root 即 /root/workspace，E2B 即 /home/user/workspace）。主 agent
  E2B 验收抓到：非 root 沙箱建 `/workspace` 报 Errno 13，turn 直接 error。
  `ROOST_WORKSPACE_DIR` 覆盖行为不变（覆盖值同样 expanduser）。
- **控制面暴露安全增补**：E2B 公开端口 URL 任何人可达，而它就是 driver 控制面。
  E2BSandboxBackend 默认 `allow_public_traffic=False`，每个 request 携带
  `e2b-traffic-access-token`（实测无 token 403）；`True` 需显式声明。
- `sandbox_timeout` 构造参数：E2B 沙箱存活时限（SDK 默认 300s 会回收），
  create/connect 均带上（connect 只延长不缩短）；长会话宿主必须显式设置。
- `pause` 后丢弃缓存的 SDK 沙箱对象、后续操作一律经 connect——"connect 隐含
  恢复"在有对象缓存 backend 上的落地方式。
- bind_host 实测结论：E2B 端口代理可达沙箱内 127.0.0.1，`DEFAULT_BIND_HOST`
  取 127.0.0.1。SDK 版本 e2b 2.39.1。

## 附录 K：M3c Claude Agent SDK harness 契约（2026-08-17 钉定）

- **依赖归属**：SDK 及其运行时（node、Claude CLI、claude-agent-sdk）预装在
  沙箱镜像/模板里，cold boot 不做包安装（热路径纪律）。交付
  `examples/sandbox-images/claude/Dockerfile`（基于 python:3.12-slim 增装，
  安装命令以官方文档核实为准）；E2B custom template 做法写进同目录 README。
  driver 本体保持 stdlib-only 不变。
- **harness 选择**：driver 启动读 `ROOST_HARNESS`（`module:attr` 工厂），
  默认 `roost.driver.harness:EchoHarness`；不可导入/实例化失败 → driver 启动
  失败（宿主按 boot 失败处理）。
- **Claude harness**：`src/roost/harness_claude.py`，惰性 import
  claude-agent-sdk（未装时实例化报清晰错误）。行为：以工作区目录为 cwd 运行
  SDK 会话并**跨 turn 续接同一会话**（会话状态落在工作区内 → 快照/恢复/替换
  自动携带对话记忆）；流式文本 → Delta、工具调用 → ToolEvent(start/result)、
  结束 → Terminal(ok + usage 透传)；SDK 异常交 worker 兜底。SDK 的 API 形态
  （query/resume/streaming 消息类型）以官方文档核实，不凭记忆。
- **凭据**：`ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` 由宿主经
  SessionContextProvider env（或 demo 装配）注入沙箱，库与 driver 不解释、
  不落日志。
- **demo 装配**：`examples/cli_chat.py` 增 `--harness {echo,claude}`；claude
  分支要求镜像参数并把本地环境的 ANTHROPIC_* 透传进 boot env。
- **测试**：harness 单元层用 fake SDK 客户端测事件映射与会话续接参数；真 LLM
  e2e 门控 `ROOST_CLAUDE_E2E=1` + `ANTHROPIC_API_KEY`（默认 skip，key 到位后
  作为 M3c 验收跑通：真 agent 在 Docker 镜像里回答 + /kill 后记忆延续）。

## 附录 K 增补：M3c 落地裁定（2026-08-17）

- claude-agent-sdk（0.2.139）wheel 自带原生 Claude Code 二进制（2.1.233），
  镜像无需 Node/npm；跨目录会话 resume 依赖 bundled CLI ≥ 2.1.223（root 与
  E2B 的 workspace 绝对路径不同，记忆跨用户迁移靠它），已写模块 docstring。
- 会话连续性：turn = 一次 query()，resume id 存 `<workspace>/.roost/claude-session`，
  `CLAUDE_CONFIG_DIR` 指向 `<workspace>/.claude`——transcript 随 M4 快照走。
  刻意不用 continue_conversation。
- Delta 粒度 = 每个 assistant TextBlock 一条（不开 token 级 partial stream，
  宿主有需求再立项）。
- permission_mode 默认 bypassPermissions（沙箱即权限边界），
  `ROOST_CLAUDE_PERMISSION_MODE` 可覆盖。
- 真 LLM e2e 门控 ROOST_CLAUDE_E2E=1 + ANTHROPIC_API_KEY，key 到位后作为
  M3c 验收（真 agent 对话 + /kill 后记忆延续）。
