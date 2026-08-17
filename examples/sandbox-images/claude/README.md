# Claude Agent SDK 沙箱镜像

`roost.harness_claude:ClaudeHarness` 运行所需的依赖**归镜像所有**（CONTRACTS.md
附录 K）：cold boot 只上传 driver 源码、不装包，所以 SDK 与 Claude CLI 必须已经
躺在镜像/模板里。这个目录就是那份预装的参考实现。

## Docker

```bash
docker build -t roost-claude examples/sandbox-images/claude/

export ANTHROPIC_API_KEY=…              # 宿主环境；由 demo 透传进沙箱 boot env
.venv/bin/python examples/cli_chat.py \
    --harness claude --image roost-claude \
    --message "read the files in your workspace and tell me what you see"
```

镜像里有什么，以及为什么：

| 内容 | 理由 |
| --- | --- |
| `python:3.12-slim` | driver 用 `python -m roost.driver` 起，镜像必须自带 `python`。 |
| `claude-agent-sdk==0.2.139` | harness 惰性 import 的那个包。 |
| 随 wheel 附带的原生 `claude` 二进制 | SDK 用子进程驱动 CLI；平台 wheel 已经带上它，因此**不需要 Node.js**。构建期有一条断言证明它在。 |
| `ENV ROOST_HARNESS=roost.harness_claude:ClaudeHarness` | 镜像即选择：宿主指定这个镜像就得到 Claude harness（仍可由 boot env 覆盖）。 |

镜像刻意**不含** `git`、编译器一类工具。要让 agent 用它们，就在这份 Dockerfile
上加一层 `apt-get install`——沙箱里能做什么由镜像定义，而不是由 harness 定义。

## E2B custom template

E2B 的模板就是"预先构建好的沙箱镜像"，做法与 Docker 版同源：

```bash
cd examples/sandbox-images/claude
e2b template build --name roost-claude --dockerfile Dockerfile
# 构建输出里的 template id 用作 --template
.venv/bin/python examples/cli_chat.py \
    --harness claude --backend e2b --template roost-claude \
    --sandbox-timeout 900
```

两点与 Docker 不同：

- **用户不是 root**：E2B 沙箱默认以 `user` 运行，工作区因此落在
  `/home/user/workspace`（附录 J 增补）。harness 把会话状态写在工作区里，
  路径差异由 `~` 解析吸收，无需额外配置。
- **不要把凭据烤进模板**：`ANTHROPIC_API_KEY` 由宿主经 cold boot env 注入
  （附录 K），模板里出现 key 意味着任何拿到模板的人都拿到了它。

## 凭据

`ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` 由宿主注入沙箱进程环境
（`examples/cli_chat.py --harness claude` 从本机环境透传；真实宿主走
`SessionContextProvider.cold_boot_context` 的 `env`）。roost 不解释、不落日志、
不写进快照。

## 会话记忆落在哪里

harness 把 `CLAUDE_CONFIG_DIR` 指到工作区里的 `.claude/`，会话 id 写在
`.roost/claude-session`。两者都在工作区内，于是被 `/v1/workspace` 的快照/恢复
自动携带——沙箱被杀掉重生后，对话记忆跟着工作区回来。
