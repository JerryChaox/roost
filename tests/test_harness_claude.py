"""ClaudeHarness 的事件映射与会话续接（fake SDK，不联网、不需要 key）。

护的是**跨层契约**，不是实现细节：

- SDK 消息 → DriverEvent 的映射（宿主看到的显示流形状）；
- 跨 turn 续接：会话 id 落在工作区里，下一 turn 以它填 `resume`；
- 事件与 Terminal.usage 必须能过 JSON wire（driver → host 是 JSON 编解码）。

fake 的形状对齐 claude-agent-sdk 0.2.139 的公开 dataclass 字段（模块 docstring
里记了核实来源）；harness 用注入进来的 namespace 做 isinstance 判定，因此这里
无需 monkeypatch sys.modules。
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from roost.driver.harness import load_harness, HarnessLoadError
from roost.events import Delta, Terminal, ToolEvent
from roost.harness_claude import (
    SESSION_ID_FILE,
    ClaudeHarness,
    ClaudeSDKUnavailableError,
    load_sdk,
)
from roost.types import TurnEnvelope

# ---- fake SDK（形状对齐真包，字段名逐一核实过） ---------------------------


@dataclass
class FakeTextBlock:
    text: str


@dataclass
class FakeToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class FakeToolResultBlock:
    tool_use_id: str
    content: Any
    is_error: bool = False


@dataclass
class FakeAssistantMessage:
    content: list[Any]


@dataclass
class FakeUserMessage:
    content: Any


@dataclass
class FakeSystemMessage:
    subtype: str
    data: dict[str, Any]


@dataclass
class FakeResultMessage:
    subtype: str = "success"
    is_error: bool = False
    session_id: str = "sess-1"
    result: str | None = "done"
    usage: dict[str, Any] = field(default_factory=lambda: {"input_tokens": 11})
    total_cost_usd: float | None = 0.01
    num_turns: int = 1
    duration_ms: int = 42


@dataclass
class FakeOptions:
    cwd: str | None = None
    permission_mode: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    resume: str | None = None
    model: str | None = None
    max_turns: int | None = None


def fake_sdk(script: list[Any]) -> SimpleNamespace:
    """把一串消息做成一个可注入的 SDK namespace，并记录每次 query 的参数。"""
    calls: list[dict[str, Any]] = []

    async def query(*, prompt: str, options: Any = None, transport: Any = None):
        calls.append({"prompt": prompt, "options": options})
        for message in script:
            yield message

    return SimpleNamespace(
        query=query,
        ClaudeAgentOptions=FakeOptions,
        AssistantMessage=FakeAssistantMessage,
        UserMessage=FakeUserMessage,
        SystemMessage=FakeSystemMessage,
        ResultMessage=FakeResultMessage,
        TextBlock=FakeTextBlock,
        ToolUseBlock=FakeToolUseBlock,
        ToolResultBlock=FakeToolResultBlock,
        calls=calls,
    )


def turn(turn_id: str = "t1", **payload: Any) -> TurnEnvelope:
    return TurnEnvelope(turn_id=turn_id, session_id="s1", payload=payload or {"text": "hi"})


async def run(harness: ClaudeHarness, envelope: TurnEnvelope) -> list[Any]:
    events: list[Any] = []
    await harness.run(envelope, events.append)
    return events


# ---- 事件映射 -------------------------------------------------------------


async def test_maps_text_tool_use_result_and_terminal(tmp_path: Path) -> None:
    sdk = fake_sdk(
        [
            FakeSystemMessage(subtype="init", data={"session_id": "sess-9"}),
            FakeAssistantMessage(
                content=[
                    FakeTextBlock(text="thinking out loud"),
                    FakeToolUseBlock(id="tu-1", name="Read", input={"path": "a.py"}),
                ]
            ),
            FakeUserMessage(
                content=[FakeToolResultBlock(tool_use_id="tu-1", content="file body")]
            ),
            FakeAssistantMessage(content=[FakeTextBlock(text="answer")]),
            FakeResultMessage(session_id="sess-9"),
        ]
    )
    harness = ClaudeHarness(workspace_dir=tmp_path, sdk=sdk, env={})

    events = await run(harness, turn())

    assert [type(event) for event in events] == [
        Delta,
        ToolEvent,
        ToolEvent,
        Delta,
        Terminal,
    ]
    assert [event.text for event in events if isinstance(event, Delta)] == [
        "thinking out loud",
        "answer",
    ]
    start, result = (event for event in events if isinstance(event, ToolEvent))
    assert (start.name, start.phase) == ("Read", "start")
    assert start.detail["input"] == {"path": "a.py"}
    # 工具结果的名字由同一 turn 里的 tool_use 反查出来——宿主渲染 "Read 完成"
    # 靠的就是这一步，SDK 的结果块本身只带 id。
    assert (result.name, result.phase) == ("Read", "result")
    assert result.detail["preview"] == "file body"
    assert result.detail["is_error"] is False

    terminal = events[-1]
    assert terminal.status == "ok"
    assert terminal.error is None
    assert terminal.usage["session_id"] == "sess-9"
    assert terminal.usage["usage"] == {"input_tokens": 11}


async def test_error_result_becomes_error_terminal(tmp_path: Path) -> None:
    sdk = fake_sdk([FakeResultMessage(subtype="error_max_turns", is_error=True, result="nope")])
    harness = ClaudeHarness(workspace_dir=tmp_path, sdk=sdk, env={})

    terminal = (await run(harness, turn()))[-1]

    assert isinstance(terminal, Terminal)
    assert terminal.status == "error"
    assert terminal.error == "nope"


async def test_events_survive_json_wire(tmp_path: Path) -> None:
    """detail/usage 里塞进不可序列化的东西也不能把 wire 编码搞崩。"""
    sdk = fake_sdk(
        [
            FakeAssistantMessage(
                content=[FakeToolUseBlock(id="tu-1", name="Bash", input={"cmd": object()})]
            ),
            FakeUserMessage(
                content=[
                    FakeToolResultBlock(
                        tool_use_id="tu-1", content=[{"type": "text", "text": "ok"}]
                    )
                ]
            ),
            FakeResultMessage(usage={"nested": {"weird": object()}}),
        ]
    )
    harness = ClaudeHarness(workspace_dir=tmp_path, sdk=sdk, env={})

    for event in await run(harness, turn()):
        json.dumps(getattr(event, "detail", None) or getattr(event, "usage", {}))


async def test_sdk_exception_is_left_to_worker(tmp_path: Path) -> None:
    """SDK 炸了就让它炸出去：Terminal 的兜底是 worker 的职责（附录 B）。"""

    async def query(*, prompt: str, options: Any = None, transport: Any = None):
        raise RuntimeError("cli exploded")
        yield  # pragma: no cover —— 让它成为一个 async generator

    sdk = fake_sdk([])
    sdk.query = query
    harness = ClaudeHarness(workspace_dir=tmp_path, sdk=sdk, env={})

    with pytest.raises(RuntimeError, match="cli exploded"):
        await run(harness, turn())


# ---- 会话续接 -------------------------------------------------------------


async def test_first_turn_has_no_resume_and_persists_session_id(tmp_path: Path) -> None:
    sdk = fake_sdk([FakeResultMessage(session_id="sess-A")])
    harness = ClaudeHarness(workspace_dir=tmp_path, sdk=sdk, env={})

    await run(harness, turn())

    options = sdk.calls[0]["options"]
    assert options.resume is None
    assert options.cwd == str(tmp_path)
    # 会话状态必须落在工作区里，快照/恢复才会带上它（附录 K）。
    assert options.env["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".claude")
    assert (tmp_path / SESSION_ID_FILE).read_text(encoding="utf-8").strip() == "sess-A"


async def test_second_turn_resumes_persisted_session(tmp_path: Path) -> None:
    sdk = fake_sdk([FakeResultMessage(session_id="sess-A")])
    harness = ClaudeHarness(workspace_dir=tmp_path, sdk=sdk, env={})
    await run(harness, turn("t1"))

    await run(harness, turn("t2", text="follow up"))

    assert sdk.calls[1]["options"].resume == "sess-A"
    assert sdk.calls[1]["prompt"] == "follow up"


async def test_resume_survives_a_fresh_harness_over_the_same_workspace(
    tmp_path: Path,
) -> None:
    """新沙箱 = 新进程 + 恢复出来的工作区：续接不能依赖进程内存。"""
    first = ClaudeHarness(
        workspace_dir=tmp_path, sdk=fake_sdk([FakeResultMessage(session_id="sess-B")]), env={}
    )
    await run(first, turn("t1"))

    sdk = fake_sdk([FakeResultMessage(session_id="sess-B")])
    reborn = ClaudeHarness(workspace_dir=tmp_path, sdk=sdk, env={})
    await run(reborn, turn("t2", text="still there?"))

    assert sdk.calls[0]["options"].resume == "sess-B"


async def test_non_text_payload_becomes_json_prompt(tmp_path: Path) -> None:
    sdk = fake_sdk([FakeResultMessage()])
    harness = ClaudeHarness(workspace_dir=tmp_path, sdk=sdk, env={})

    await run(harness, turn("t1", counter=True))

    assert json.loads(sdk.calls[0]["prompt"]) == {"counter": True}


# ---- 配置面与依赖缺失 -----------------------------------------------------


async def test_env_configures_model_permission_and_max_turns(tmp_path: Path) -> None:
    sdk = fake_sdk([FakeResultMessage()])
    harness = ClaudeHarness(
        workspace_dir=tmp_path,
        sdk=sdk,
        env={
            "ROOST_CLAUDE_MODEL": "claude-sonnet-4-5",
            "ROOST_CLAUDE_PERMISSION_MODE": "acceptEdits",
            "ROOST_CLAUDE_MAX_TURNS": "3",
        },
    )

    await run(harness, turn())

    options = sdk.calls[0]["options"]
    assert (options.model, options.permission_mode, options.max_turns) == (
        "claude-sonnet-4-5",
        "acceptEdits",
        3,
    )


def test_default_permission_mode_is_bypass(tmp_path: Path) -> None:
    harness = ClaudeHarness(workspace_dir=tmp_path, sdk=fake_sdk([]), env={})
    assert harness._permission_mode == "bypassPermissions"


def test_missing_sdk_raises_a_clear_error() -> None:
    with pytest.raises(ClaudeSDKUnavailableError, match="claude-agent-sdk"):
        load_sdk("roost_no_such_sdk_module")


def test_incomplete_sdk_names_are_reported() -> None:
    with pytest.raises(ClaudeSDKUnavailableError, match="query"):
        load_sdk("json")


def test_load_harness_wraps_instantiation_failure() -> None:
    """未装 SDK 的沙箱里选了 claude harness = driver 启动失败，不是静默降级。"""
    if importlib.util.find_spec("claude_agent_sdk") is not None:
        pytest.skip("本机装了 claude-agent-sdk，实例化不会失败")
    with pytest.raises(HarnessLoadError):
        load_harness("roost.harness_claude:ClaudeHarness")
