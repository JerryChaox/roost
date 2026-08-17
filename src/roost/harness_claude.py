"""ClaudeHarness —— 把 Claude Agent SDK 接成 driver 的 Harness（M3c）。

契约见 CONTRACTS.md《附录 K：M3c Claude Agent SDK harness 契约》。本模块只承担
一类职责：**把 SDK 的消息流翻译成 DriverEvent，并让会话状态落在工作区里**。
沙箱怎么起、事件怎么送回宿主、turn 怎么去重，都不在这里。

## 依赖归属

SDK 与它的运行时**预装在镜像/模板里**（`examples/sandbox-images/claude/`），
cold boot 不做包安装（热路径纪律）。因此这里的 import 是**惰性**的：
`roost` 包本体仍然零运行时依赖，只有真的选了这个 harness 时才需要 SDK 在场；
没装时在**实例化**（而不是 import 本模块）时抛 `ClaudeSDKUnavailableError`，
driver 启动因此明确失败，宿主按 boot 失败处理。

## 跨 turn 的会话续接（本模块的核心设计）

一个 turn = 一次 `query()` 调用。续接靠两样东西，两样**都在工作区里**，因此
被 `/v1/workspace` 的快照/恢复自动携带（附录 G）——沙箱被杀、被替换、被冷启到
另一台机器上，对话记忆跟着工作区走：

1. **transcript**：把 `CLAUDE_CONFIG_DIR` 指到 `<workspace>/.claude`，SDK 的
   session jsonl 于是落在 `<workspace>/.claude/projects/<encoded-cwd>/`，
   而不是沙箱的 `~/.claude`（后者不在备份范围里，一次沙箱重生就丢光）。
2. **session id**：`ResultMessage.session_id` 写进 `<workspace>/.roost/
   claude-session`，下一个 turn 用它填 `ClaudeAgentOptions.resume`。

`continue_conversation`（"目录里最近的那个会话"）刻意不用：宿主可能把多个
session 的工作区搞混，显式 id 才是可解释的续接。

已知边界：transcript 目录名由 **cwd 的绝对路径**编码而来，工作区在新沙箱里
换了绝对路径（root 的 /root/workspace vs 非 root 的 /home/user/workspace）时，
恢复出来的目录名与新 cwd 不一致。Claude Code v2.1.223 起 resume 会跨目录查找
session id，这条路径因此仍然成立；更早的 CLI bundle 只在当前项目目录里找。
（来源：官方 Agent SDK Sessions 文档 "Resume across hosts" / "Resume by ID"。）

## 事件映射

| SDK 消息 | DriverEvent |
| --- | --- |
| `AssistantMessage` 里的 `TextBlock` | `Delta(text=block.text)` |
| `AssistantMessage` 里的 `ToolUseBlock` | `ToolEvent(phase="start")` |
| `UserMessage` 里的 `ToolResultBlock` | `ToolEvent(phase="result")` |
| `ResultMessage` | `Terminal(status, usage 透传)` |

Delta 的颗粒是**一个 assistant 文本块**而不是 token：`include_partial_messages`
的 `StreamEvent` 是可选的更细颗粒，但它带来一套 partial/最终消息去重的状态，
而事件流的语义（宿主拿到的是可拼接的文本片段）在块颗粒上已经成立。

SDK 抛异常时**不接住**：worker 的兜底会给出 `Terminal(status="error")`
（附录 B）。这里接住只会把一次可解释的失败变成一条更模糊的自造错误。

凭据（`ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`）由宿主经 cold boot env 注入
进程环境，SDK 自己读；本模块**不解释、不转发、不落日志**。

## 核实过的 SDK API（不凭记忆）

对照 claude-agent-sdk 0.2.139 与官方文档
（https://code.claude.com/docs/en/agent-sdk/python 与 .../sessions）：

- `query(*, prompt, options=None, transport=None) -> AsyncIterator[Message]`
- `ClaudeAgentOptions(cwd=..., resume=..., permission_mode=..., env=...,
  model=..., max_turns=..., setting_sources=...)`
- `AssistantMessage.content` / `UserMessage.content`：内容块列表
- `TextBlock.text`、`ToolUseBlock.id|name|input`、
  `ToolResultBlock.tool_use_id|content|is_error`
- `ResultMessage.session_id|subtype|is_error|result|usage|total_cost_usd|
  num_turns|duration_ms`
- `SystemMessage.subtype|data`（init 消息的 session_id 嵌在 `data` 里）
"""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .driver.workspace import workspace_dir_from_env
from .events import Delta, DriverEvent, Terminal, ToolEvent
from .protocol import ENV_PREFIX
from .types import TurnEnvelope

__all__ = [
    "ClaudeHarness",
    "ClaudeSDKUnavailableError",
    "load_sdk",
    "SDK_MODULE",
    "SESSION_ID_FILE",
    "CONFIG_DIR_NAME",
    "ENV_MODEL",
    "ENV_PERMISSION_MODE",
    "ENV_MAX_TURNS",
    "DEFAULT_PERMISSION_MODE",
]

SDK_MODULE = "claude_agent_sdk"

#: 会话 id 的落点（工作区内相对路径）——跟着工作区快照走。
SESSION_ID_FILE = ".roost/claude-session"
#: SDK 的配置/transcript 目录（工作区内相对路径），经 CLAUDE_CONFIG_DIR 指过去。
CONFIG_DIR_NAME = ".claude"
ENV_CLAUDE_CONFIG_DIR = "CLAUDE_CONFIG_DIR"

ENV_MODEL = f"{ENV_PREFIX}CLAUDE_MODEL"
ENV_PERMISSION_MODE = f"{ENV_PREFIX}CLAUDE_PERMISSION_MODE"
ENV_MAX_TURNS = f"{ENV_PREFIX}CLAUDE_MAX_TURNS"

# 沙箱就是隔离边界，进程内再摆一道"人来点确认"的门没有意义——没有人在那头。
DEFAULT_PERMISSION_MODE = "bypassPermissions"

STATUS_OK = "ok"
STATUS_ERROR = "error"
PHASE_START = "start"
PHASE_RESULT = "result"
SUBTYPE_SUCCESS = "success"

#: 工具结果摘要的长度上限：事件是给人看的观测面，不是数据管道。
TOOL_RESULT_PREVIEW_CHARS = 2000

_SDK_NAMES = (
    "query",
    "ClaudeAgentOptions",
    "AssistantMessage",
    "UserMessage",
    "SystemMessage",
    "ResultMessage",
    "TextBlock",
    "ToolUseBlock",
    "ToolResultBlock",
)


class ClaudeSDKUnavailableError(RuntimeError):
    """沙箱里没有可用的 claude-agent-sdk（或版本缺了需要的名字）。"""


def load_sdk(module_name: str = SDK_MODULE) -> SimpleNamespace:
    """惰性取回本模块用到的 SDK 名字，缺一不可。

    收成一个 namespace 而不是散着 import：harness 只依赖这九个名字，
    测试因此可以传一个同形状的 fake 进来，而不必去 monkeypatch sys.modules。
    """
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:  # 未安装是最常见的一种
        raise ClaudeSDKUnavailableError(
            f"导入 {module_name} 失败：{exc}。Claude harness 要求沙箱镜像里预装 "
            "claude-agent-sdk 与 Claude CLI（见 examples/sandbox-images/claude/）。"
        ) from exc
    missing = [name for name in _SDK_NAMES if not hasattr(module, name)]
    if missing:
        raise ClaudeSDKUnavailableError(
            f"{module_name} 缺少本 harness 需要的名字：{', '.join(missing)}"
        )
    return SimpleNamespace(**{name: getattr(module, name) for name in _SDK_NAMES})


class ClaudeHarness:
    """以工作区为 cwd 跑 Claude Agent SDK，并跨 turn 续接同一个会话。

    参数（全部可选，缺省从环境变量取——沙箱里的配置面只有环境变量）：
        workspace_dir:   工作区目录，缺省 `ROOST_WORKSPACE_DIR` / `~/workspace`。
        model:           `ROOST_CLAUDE_MODEL`，缺省交给 SDK 决定。
        permission_mode: `ROOST_CLAUDE_PERMISSION_MODE`，缺省 bypassPermissions。
        max_turns:       `ROOST_CLAUDE_MAX_TURNS`，缺省不限。
        sdk:             注入的 SDK namespace（测试用）；缺省惰性 import。
        env:             读取上述环境变量的来源（测试用）。
    """

    def __init__(
        self,
        *,
        workspace_dir: str | os.PathLike[str] | None = None,
        model: str | None = None,
        permission_mode: str | None = None,
        max_turns: int | None = None,
        sdk: Any | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        source = os.environ if env is None else env
        self._workspace_dir = workspace_dir
        self._env = env
        self._model = model if model is not None else source.get(ENV_MODEL) or None
        self._permission_mode = (
            permission_mode
            if permission_mode is not None
            else source.get(ENV_PERMISSION_MODE) or DEFAULT_PERMISSION_MODE
        )
        self._max_turns = (
            max_turns if max_turns is not None else _int_or_none(source.get(ENV_MAX_TURNS))
        )
        # 惰性 import 发生在**实例化**时：driver 启动即失败，而不是等到第一个
        # turn 才在 harness 里炸——后者会让一个配置错误看起来像一次模型失败。
        self._sdk = load_sdk() if sdk is None else sdk

    # -- Harness 接口 ----------------------------------------------------

    async def run(self, turn: TurnEnvelope, emit: Callable[[DriverEvent], None]) -> None:
        workspace = self._workspace()
        workspace.mkdir(parents=True, exist_ok=True)
        prompt = _prompt_text(turn.payload)
        resumed = self._read_session_id(workspace)
        options = self._options(workspace, resume=resumed)

        tool_names: dict[str, str] = {}
        session_id = resumed
        async for message in self._sdk.query(prompt=prompt, options=options):
            captured = self._capture_session_id(message)
            if captured:
                session_id = captured
            for event in self._map(message, turn, tool_names):
                emit(event)
        # 会话 id 落盘放在流结束后：query() 正常跑完时 ResultMessage 一定到过，
        # 而中途异常的那一次没有可信的新 id（写进去只会让下一 turn resume 到一个
        # 半截会话）。异常路径的终态由 worker 兜底。
        if session_id and session_id != resumed:
            self._write_session_id(workspace, session_id)

    # -- SDK 调用面 ------------------------------------------------------

    def _options(self, workspace: Path, *, resume: str | None) -> Any:
        kwargs: dict[str, Any] = {
            "cwd": str(workspace),
            "permission_mode": self._permission_mode,
            # transcript 落进工作区 → 跟着快照走（见模块 docstring）。
            "env": {ENV_CLAUDE_CONFIG_DIR: str(workspace / CONFIG_DIR_NAME)},
        }
        if resume:
            kwargs["resume"] = resume
        if self._model:
            kwargs["model"] = self._model
        if self._max_turns is not None:
            kwargs["max_turns"] = self._max_turns
        return self._sdk.ClaudeAgentOptions(**kwargs)

    # -- 事件映射 --------------------------------------------------------

    def _map(
        self, message: Any, turn: TurnEnvelope, tool_names: dict[str, str]
    ) -> list[DriverEvent]:
        sdk = self._sdk
        if isinstance(message, sdk.AssistantMessage):
            return self._map_assistant(message, turn, tool_names)
        if isinstance(message, sdk.UserMessage):
            return self._map_user(message, turn, tool_names)
        if isinstance(message, sdk.ResultMessage):
            return [self._map_result(message, turn)]
        # SystemMessage / StreamEvent / 未来新增的消息类型：没有对应的显示语义，
        # 静默跳过。事件流是给宿主渲染的，不是 SDK 的镜像。
        return []

    def _map_assistant(
        self, message: Any, turn: TurnEnvelope, tool_names: dict[str, str]
    ) -> list[DriverEvent]:
        sdk = self._sdk
        events: list[DriverEvent] = []
        for block in getattr(message, "content", ()) or ():
            if isinstance(block, sdk.TextBlock):
                if block.text:
                    events.append(Delta(turn_id=turn.turn_id, text=block.text, seq=0))
            elif isinstance(block, sdk.ToolUseBlock):
                tool_names[block.id] = block.name
                events.append(
                    ToolEvent(
                        turn_id=turn.turn_id,
                        name=block.name,
                        phase=PHASE_START,
                        detail={
                            "tool_use_id": block.id,
                            "input": _jsonable(block.input),
                        },
                        seq=0,
                    )
                )
        return events

    def _map_user(
        self, message: Any, turn: TurnEnvelope, tool_names: dict[str, str]
    ) -> list[DriverEvent]:
        sdk = self._sdk
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            return []
        events: list[DriverEvent] = []
        for block in content:
            if not isinstance(block, sdk.ToolResultBlock):
                continue
            events.append(
                ToolEvent(
                    turn_id=turn.turn_id,
                    # 名字来自同一 turn 里的 tool_use；对不上时也不丢事件。
                    name=tool_names.get(block.tool_use_id, "unknown"),
                    phase=PHASE_RESULT,
                    detail={
                        "tool_use_id": block.tool_use_id,
                        "is_error": bool(getattr(block, "is_error", False)),
                        "preview": _preview(block.content),
                    },
                    seq=0,
                )
            )
        return events

    def _map_result(self, message: Any, turn: TurnEnvelope) -> Terminal:
        subtype = getattr(message, "subtype", None)
        is_error = bool(getattr(message, "is_error", False)) or (
            subtype is not None and subtype != SUBTYPE_SUCCESS
        )
        result = getattr(message, "result", None)
        usage: dict[str, Any] = {"subtype": subtype}
        for field in ("session_id", "num_turns", "duration_ms", "total_cost_usd", "usage"):
            value = getattr(message, field, None)
            if value is not None:
                usage[field] = _jsonable(value)
        return Terminal(
            turn_id=turn.turn_id,
            status=STATUS_ERROR if is_error else STATUS_OK,
            error=(str(result) if result else str(subtype)) if is_error else None,
            usage=usage,
            seq=0,
        )

    def _capture_session_id(self, message: Any) -> str | None:
        """会话 id：ResultMessage 上恒有；init 的 SystemMessage 里也带一份。"""
        sdk = self._sdk
        if isinstance(message, sdk.ResultMessage):
            value = getattr(message, "session_id", None)
            return value if isinstance(value, str) and value else None
        if isinstance(message, sdk.SystemMessage):
            data = getattr(message, "data", None)
            if isinstance(data, dict):
                value = data.get("session_id")
                return value if isinstance(value, str) and value else None
        return None

    # -- 工作区内的会话状态 ----------------------------------------------

    def _workspace(self) -> Path:
        if self._workspace_dir is not None:
            return Path(self._workspace_dir).expanduser()
        return workspace_dir_from_env(self._env)

    def _read_session_id(self, workspace: Path) -> str | None:
        path = workspace / SESSION_ID_FILE
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    def _write_session_id(self, workspace: Path, session_id: str) -> None:
        path = workspace / SESSION_ID_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{session_id}\n", encoding="utf-8")
        except OSError:
            # 写不下去只是"下一 turn 从头开始"，不该把一个已经答完的 turn 变成失败。
            pass


# -- 模块级小工具 --------------------------------------------------------


def _prompt_text(payload: dict[str, Any]) -> str:
    """turn payload → prompt。`text` 是约定字段，其余原样 JSON 化交给模型。"""
    text = payload.get("text")
    if isinstance(text, str):
        return text
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _int_or_none(raw: str | None) -> int | None:
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _jsonable(value: Any) -> Any:
    """事件 detail/usage 要过 JSON wire：不可序列化的一律降级成字符串。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return repr(value)


def _preview(content: Any) -> str:
    """工具结果 → 有界的一段文本。

    content 可能是字符串，也可能是内容块列表（每块 dict 或 dataclass）。
    这里只取得出文本的部分，取不出就用 repr——观测面不该因为一个陌生的块类型
    而丢事件。
    """
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", item)))
            else:
                parts.append(str(getattr(item, "text", None) or repr(item)))
        text = "\n".join(parts)
    elif content is None:
        text = ""
    else:
        text = repr(content)
    if len(text) > TOOL_RESULT_PREVIEW_CHARS:
        return text[:TOOL_RESULT_PREVIEW_CHARS] + "…"
    return text
