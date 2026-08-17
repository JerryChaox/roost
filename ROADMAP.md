# roost — 里程碑路线

原则：每个里程碑结束时都有一个可演示的行为；三条不变量（DESIGN.md §2）各有专属验收
demo。M1–M6 是串行主线，M7 起并行度放开。

另一条明文原则——**反腐化拆分**：本项目是对生产实现的重写移植，来源代码里的巨石文件
（数千行的 driver、近九千行的 outbound）是"热路径上只敢追加"的产物，不是设计。
移植时按设计与约束做职责拆分：一个模块只承担一类职责、只因一类原因变更；
协议编解码、状态机、IO 边界不得混居一个文件。

| # | 里程碑 | 交付物 | 验收标准 |
|---|---|---|---|
| M0 | 骨架 ✅ | DESIGN / CONTRACTS / 类型化 ports | 完成（`5bd0f4f`） |
| M1 | 状态与投递内核 | SQLite StateStore、进程内 TurnDelivery、turn pipeline（begin/renew/finish/sweep、session 串行门） | fake sandbox 下 sender 侧幂等成立：并发重复 enqueue 同一 turn_id，恰好一次 begin 成功；建立 StateStore 契约测试套件（CAS/锁语义，约束所有未来实现） |
| M2 | driver 子系统与控制协议 | 沙箱侧 driver（多模块，见下）、宿主侧 control client、PROTOCOL.md 定稿、协议测试移植 | driver 在裸容器通过协议测试：`POST /v1/turn` 重复提交返回既有 entry 不重跑（I1 driver 侧） |
| M3 | Docker backend 与首个端到端 | 本地 Docker SandboxBackend、cold boot 编排、事件 reducer、CLI demo 宿主 | **Demo 1：at-least-once 投递下 exactly-once 执行**——人为双投，agent 只答一次 |
| M4 | Durability | SnapshotStore（本地 FS + S3 兼容）、turn 边界异步 backup、恢复路径、boot 失败可观测 | **Demo 2：`docker kill` 沙箱 → 下一条消息自动重建，会话状态还在**（I2） |
| M5 | Watchdog 与 liveness | watch 轮次、heartbeat、turn_age 判定、sweep requeue | 注入 hang：watchdog 识别并 requeue 到新沙箱，turn 最终有答复；idle 长 turn 不误杀 |
| M6 | 零停机 forced update | runtime fingerprint、内存快照换绑、失败回退 + backoff | **Demo 3：对话中升级 runtime 用户无感；注入升级失败，回退旧沙箱正常应答**（I3） |
| M7 | E2B backend | E2B SandboxBackend（create / connect 隐含恢复 / pause） | 三个 demo 在 E2B 原样通过——backend 可插拔实证 |
| M8 | 0.1 公开发布 | README 完整（三 demo 录屏）、PROTOCOL.md 定稿、示例宿主、CI、PyPI | `pip install roost` 十分钟跑通 Demo 1；公开发布贴 |
| M9 | 生产反向采纳 | 上游宿主（museon agents）以 roost 为依赖实现六 port，上 staging | 真实 IM 对话跑在 roost 内核上——生产级 API 硬化。**前置：M11、M12** |
| M10 | 生态扩展 | IM 桥可选 backend、Cloud Tasks / GCS adapter 小包、第二 harness | 按需求排优先级，不预先承诺 |
| M11 | 多消费者与 Postgres | session 级 advisory lock（关闭 M1 的单消费者串行化边界）、Postgres StateStore | 多 worker 并发消费下同 session 两个不同 turn 不并跑（既有契约套件跑通 Postgres 实现） |
| M12 | 生产级 watchdog 语义 | 以生产实现为 spec 逐条复刻：watch 状态机（密集/long-watch 节奏）、silence/syscall defer、turn_age 判定、kill/requeue 决策面、零写入观测纪律 | 每条语义带上其对应的生产事故动机；spec 逐条核对通过。生产 watchdog 跑的都是实际问题，不做"按需再长"的赌注 |

## M2 目标模块结构（预铺，开工时细化）

"单文件"仅指分发形态：源码为正常多模块包，构建时打成一个自包含 artifact 注入沙箱
（沙箱内零安装依赖，fingerprint 对 artifact 计算）。源码结构：

```
src/roost/driver/            # 沙箱侧（打包进 artifact）
  server.py                  # loopback HTTP control server（仅路由与编解码）
  registry.py                # turn registry —— I1 driver 侧幂等，独立可测
  worker.py                  # session worker run loop（排队、恢复语义）
  harness.py                 # harness adapter（Claude Agent SDK 调用生命周期）
  emit.py                    # 事件产生与 wire 序列化
  update.py                  # 运行时自更新
src/roost/control/           # 宿主侧
  envelope.py                # envelope 编解码（driver 打包时复用同一实现）
  client.py                  # 协议客户端（turn 提交 / health / 事件消费 / 超时重试）
```

排序依据：M1 先于 M2——sender 侧幂等可用 fake sandbox 独立验证；M4 先于 M5/M6——
requeue 与 forced update 都依赖 snapshot 恢复路径；M7 后置——先在 Docker 上把语义
打磨对，再证明可插拔。M8 不等 M9：发布不依赖上游迁移。
