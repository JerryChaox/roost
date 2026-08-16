# roost — 奠基设计

状态：v1 草案（2026-08-16 起草，2026-08-17 定名 roost）。本文档是新仓的第一份文档，
定义心智模型、架构抽象和协议契约。名字隐喻：session 像候鸟，飞到哪都能落脚栖息，
沙箱只是今晚的栖木。来源事实基于 museon apps/agents 生产实现的依赖图分析。

## 一、这个库回答什么问题

> 如何让一个由消息触发的 AI agent，在一个可销毁的远程沙箱里，表现得像一个
> **永不丢消息、永不重复执行、可随时升级的长期会话进程**。

不是"如何开一个沙箱"（E2B/Modal 已解决），而是沙箱之上缺失的会话运行时语义。

## 二、心智模型

### 核心概念（ubiquitous language）

| 概念 | 定义 | 关键性质 |
|---|---|---|
| **Session** | 长期存在的逻辑会话，agent 状态的所有者 | `session_id` 对库是 opaque 字符串；宿主决定它映射什么（Museon 里是 conversation） |
| **Sandbox** | 可销毁/可暂停/可重建的计算环境实例 | Session↔Sandbox 是"一对至多一"绑定；sandbox 是 session 状态的**物化缓存**，不是 source of truth |
| **Snapshot** | session 持久化状态（workspace 字节） | 存于 SnapshotStore；sandbox 死亡 → 从 snapshot 重建 = session 不死 |
| **Turn** | 一次触发到一次 agent 执行的原子单位 | `turn_id` 从触发消息**确定性派生**，是幂等主键，绝不用随机 id |
| **Driver** | 库随发布的沙箱内常驻进程 | 接收 turn、驱动 harness、上报事件流、执行自更新 |
| **Event stream** | driver 产出的有序事件 | host 侧 reducer 归约；渲染与投递归宿主 |
| **Watchdog** | host 侧对 in-flight turn 的看护 | 以 turn 年龄区分 idle 与 hang，决策 requeue / 强制重建 |
| **Runtime fingerprint** | 沙箱内运行时文件的版本指纹 | 驱动零停机 forced update 的过期判定 |

### 三条不变量（库的灵魂，一切 API 设计服从它们）

- **I1 · Exactly-once**：投递假设 at-least-once；同一 `turn_id` 无论投递多少次，
  harness 恰好执行一次。双侧共同保证——sender 侧 session 级串行门 +
  driver 侧进程内 turn registry 去重。唯一合法的重跑：宿主 watchdog 决策
  requeue 到**新** sandbox（新进程、空 registry）。
- **I2 · Durability**：session 状态在任何 sandbox 死亡、替换、升级后可恢复。
  snapshot 写在 turn 边界异步进行，写失败不影响 turn 结果。
- **I3 · Never worse off**：forced update / 替换失败时，旧 sandbox 保持绑定且可恢复，
  turn 正常应答；失败设 backoff 不重试风暴。卡死的 turn 最终被 watchdog
  识别并 requeue。

### 分层

```
宿主应用（Museon / Feishu bridge / 任何 chat 网关）
  职责：身份、路由、渲染投递、消息存储、计费
────────── ports（第三节） ──────────
runtime 库（本项目，跑在宿主进程内）
  职责：session↔sandbox registry、turn pipeline、watchdog、
        event reducer、cold boot / forced update 编排
────────── control protocol（第四节，loopback HTTP） ──────────
driver（本项目发布，跑在沙箱内）
  职责：turn registry 幂等、harness 驱动、事件上报、运行时自更新
────────── harness adapter ──────────
agent harness（默认且唯一实现：Claude Agent SDK）
```

设计纪律：**库不认识宿主的领域**。organization / actor / workspace / tenant /
conversation 这些词不出现在库的任何签名、表、协议字段里；宿主领域信息一律折进
opaque 的 `session_id`、snapshot key 和 turn context blob。

## 三、架构抽象（ports）

### 宿主提供（依赖注入）

依赖图证实现有 4 万行代码的全部 Museon 耦合面收敛为以下六个接口：

1. **TurnDelivery** — at-least-once 任务投递：`enqueue(turn, not_before)`。
   默认实现：进程内 asyncio 队列（带重试）。adapter：Cloud Tasks、任意消息队列。
   幂等由 I1 保证，所以投递层不需要 exactly-once——这是本设计最重要的解耦。
2. **StateStore** — source of truth：session↔sandbox 绑定、turn 生命周期行
   （CAS 状态转换、lease/lock）、runtime stamp。默认 Postgres，dev 用 SQLite。
   **库拥有自己的少量表**（表名前缀可配），不映射宿主表；宿主迁移时自己做对账。
3. **SnapshotStore** — `put(key, bytes)` / `get(key)`，key 是 opaque 字符串，
   由宿主的 `snapshot_key(session_id) -> str` 回调派生（现实现把 org/actor/conversation
   钉进签名，此处是主要 generic 化点）。默认实现：本地 FS；S3 兼容（覆盖 GCS HMAC）。
4. **SandboxBackend** — create / connect（暂停态隐含恢复，无独立 resume）/ pause /
   kill / exec / 文件上传 / 到 driver 控制面的 HTTP 通道。
   实现顺序：E2B 第一，本地 Docker 第二（降低尝试门槛的关键）。
5. **EventSink** — 接收 reducer 归约后的 display events / lane 状态。宿主决定渲染
   （CardKit、Slack blocks、SSE、终端）。库内只保留通用 reducer；
   现 `driver_event_outbound.py` 的 Feishu delivery 状态机整体归宿主/桥项目。
6. **SessionContextProvider** — cold boot 时库向宿主要一组
   `{files, env, skills}` 注入沙箱（承接现 museoncli token / profile skills 的耦合面）。
   语义归宿主：宿主给什么，库装什么，库不解释内容。

可选：**OpsRecorder** — fire-and-forget 观测事件（boot 失败、watchdog 决策、
forced update 结果）。默认结构化日志。设计原则沿用生产教训：绝不 await、
绝不因观测失败影响行为、纯 watch 轮次零写入。

### 库自有（不可插拔的核心价值）

- **SessionSandboxRegistry**：绑定管理、cold boot 编排、forced update 的
  "旧沙箱内存快照 → 新沙箱冷启动 → 原子换绑 → 失败回退" 全流程。
- **Turn pipeline**：确定性 turn id、session 级串行门（`has_active_turn`）、
  消息 buffer 与批量 drain、锁续期。
- **Watchdog**：watch 轮次、heartbeat、`turn_age` 判定、requeue 决策。
- **Event reducer**：driver 事件流 → display 状态（lane、lifecycle notice、terminal）。
- **Driver**：随库版本发布，进沙箱；含 turn registry、harness runner、自更新。
- **Runtime fingerprint** + 沙箱内热更新机制。

## 四、协议契约（control protocol，v3 机制 generic 化）

依赖图证实 v3 机制（loopback HTTP、file-free 控制面、session worker）自成一体，
需要 generic 化的只是 wire contract：

### Transport

driver 在沙箱内起 loopback HTTP control server；host 经 SandboxBackend 的
exec/tunnel 访问。保留 file-free 设计（控制面不落文件，不依赖沙箱文件系统权限模型）。

### 命名清洗（从 Museon 版迁移的对照）

| Museon 版 | 开源版 |
|---|---|
| `X-Museon-*` headers | `X-Roost-*` |
| envelope 里 `conversation_id` / `scope_conversation_id` / `actor_id` | `session_id` + opaque `context` blob |
| `museoncli_config` 注入字段 | SessionContextProvider 的 `files` 通道 |
| `AGENTS_SANDBOX_*` 环境变量 | `ROOST_*` |
| PROTOCOL.md 中 Museon admin endpoint / control token 章节 | 移除，换 generic auth 章节 |

### 核心端点

- `POST /v1/turn` — 提交 turn。**幂等**：body 携带 `turn_id`；重复提交返回既有
  entry，不重新入队（I1 的 driver 侧）。
- `GET /v1/health` — driver 就绪与 harness 状态。
- `POST /v1/update` — 触发运行时自更新（fingerprint 内更新，不换 sandbox）。
- 事件流：driver 推送（实现机制沿用 v3 的事件通道，具体承载在骨架实现时定）。

### Turn envelope（字段最小集）

```
turn_id: string        # 确定性派生，幂等主键
session_id: string     # opaque
attempt: int           # 投递尝试计数，观测用，不参与幂等
payload: {...}         # prompt / 消息批
context: {...}         # opaque 宿主 blob，库与 driver 均不解释
```

### 事件类型

`delta`（流式文本）、`tool_event`、`lifecycle_notice`（boot/update 进度，携带
`elapsed_ms`，支撑宿主渲染"启动中 · Ns"类状态）、`terminal`（ok/error + usage）。

### 协议中明文写死的幂等契约

PROTOCOL.md 必须包含（这是本项目区别于"又一个沙箱 SDK"的部分）：
投递假设 at-least-once；driver 以 turn_id 去重实现 exactly-once execution；
重跑仅发生于新 sandbox + 宿主显式 requeue；turn registry 是进程内状态，
其生命周期与 sandbox 进程绑定——这一约定是 I1 双侧分工的边界线。

### 版本化

协议版本走 header；运行时文件走 fingerprint。二者独立演进：协议 breaking change
才 bump 协议版本，运行时文件变更只触发 fingerprint 过期 → forced update。

## 五、与飞书桥（项目二）的接口

桥不依赖本库。共享的只有 turn 幂等契约的形状（确定性 turn id + at-least-once 假设），
桥在其本地 Claude Code executor 上自行实现同样约定；本库作为桥的可选 executor
backend 时，桥充当宿主，实现上面六个 port 中的 EventSink（CardKit 渲染）与
SessionContextProvider。早期两边各持一份接口副本，API 稳定后再抽公共包。

## 六、已知的待定项

- 事件流承载机制（v3 现实现 vs 简化）——骨架实现时定。
- StateStore 表结构（最小行集）——骨架实现时定。
- driver 分发方式（随 wheel 内嵌 vs 独立构件）——倾向 wheel 内嵌单文件。
