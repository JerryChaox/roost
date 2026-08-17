# Claude Agent SDK 沙箱镜像（CONTRACTS.md 附录 K：依赖归属）
#
# roost 的 cold boot **不装包**：driver 源码是上传进去的，SDK 与它的运行时必须
# 已经在镜像里。这个镜像就是那份"预装"，构建一次、每次 boot 直接用。
#
#   docker build -t roost-claude examples/sandbox-images/claude/
#   .venv/bin/python examples/cli_chat.py --harness claude --image roost-claude
#
# driver 本体仍然只依赖标准库；这里装的东西只服务于
# `roost.harness_claude:ClaudeHarness`。
FROM python:3.12-slim

# claude-agent-sdk 的平台 wheel **自带一个原生 Claude Code 二进制**
# （官方 Quickstart："Both the TypeScript and Python SDKs bundle a native Claude
# Code binary, so most installs need no separate Claude Code install."），因此这里
# 不需要 Node.js，也不需要 `npm install -g @anthropic-ai/claude-code`。
# 版本钉死是有意的：镜像是 boot 的热路径，"每次构建拿到不同的 agent 行为"
# 会让沙箱之间失去可比性。要升级就显式改这一行（或传 --build-arg）。
ARG CLAUDE_AGENT_SDK_VERSION=0.2.139

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir "claude-agent-sdk==${CLAUDE_AGENT_SDK_VERSION}"

# 构建期自证：SDK 装上了，且**这一平台的 wheel 真的带了原生 CLI**。
# sdist 回退（少见平台）装出来的包没有二进制，运行时才会以一个含糊的
# "找不到 claude" 失败——那时候它看起来像一次模型故障，而不是一次装配错误。
RUN python - <<'PY'
import pathlib
import subprocess
import sys

import claude_agent_sdk

bundled = pathlib.Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
if not bundled.exists():
    sys.exit(
        "claude-agent-sdk 装的是不带原生 CLI 的发行版。"
        "请改用带 CLI 的 wheel，或在镜像里另行安装 Claude Code 并让它出现在 PATH 上。"
    )
subprocess.run([str(bundled), "--version"], check=True)
PY

# 镜像默认就是 Claude harness：宿主只要指定这个镜像，driver 启动时自然选中它
# （宿主仍可用 boot env 里的 ROOST_HARNESS 覆盖）。
ENV ROOST_HARNESS=roost.harness_claude:ClaudeHarness

# 沙箱的主进程由 SandboxBackend 决定（DockerSandboxBackend 用 `sleep infinity`），
# 这里的 CMD 只是让镜像单独 `docker run` 时也有个合理的行为。
CMD ["sleep", "infinity"]
