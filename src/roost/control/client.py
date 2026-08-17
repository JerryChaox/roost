"""宿主侧协议客户端。

契约见 CONTRACTS.md《附录 B — 端点行为 / M2 交付模块》。

职责边界（刻意窄）：把四个端点翻译成方法调用，经注入的 `SandboxBackend.request`
通道收发字节，用 `envelope.py` 做编解码，把协议级失败翻译成异常。

**不做业务重试**：超时、5xx、连接失败一律原样抛给宿主。重投、requeue、backoff
是 host 侧 pipeline / watchdog 的语义（I1 只允许 requeue 到**新**沙箱），
客户端自作主张重试会在同一 driver 进程上制造第二次提交，把幂等责任推给
registry 去兜——那不是这一层该做的决定。

`duplicate` 不是错误：driver 已见过该 turn_id，按 I1 绝不重跑，客户端如实返回。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from ..events import DriverEvent
from ..ports import SandboxBackend
from ..protocol import (
    HEADER_PROTOCOL_VERSION,
    HEALTH_ENDPOINT,
    PROTOCOL_VERSION,
    TURN_ENDPOINT,
)
from ..types import SandboxHandle, TurnEnvelope
from .envelope import ProtocolError, decode_event, decode_json, encode_turn_bytes

__all__ = [
    "WORKSPACE_ENDPOINT",
    "ControlClient",
    "ControlError",
    "ControlTimeoutError",
    "ProtocolVersionError",
    "UnknownTurnError",
    "TurnSubmission",
    "EventPage",
    "HealthStatus",
    "DEFAULT_WAIT_MS",
    "MAX_WAIT_MS",
]

DEFAULT_WAIT_MS = 10_000
MAX_WAIT_MS = 30_000

# 协议 v1 的 workspace 端点（附录 G）。它的正规归宿是 `protocol.py` 里的端点常量组；
# 放在这里是因为 M4 的改动面明确不含 protocol.py，driver 侧从这里引用（driver →
# control 是既有的依赖方向：server.py 已经引 control/envelope.py），双端仍是同一份。
WORKSPACE_ENDPOINT = "/v1/workspace"
WORKSPACE_CONTENT_TYPE = "application/gzip"

STATE_ACCEPTED = "accepted"
STATE_DUPLICATE = "duplicate"


class ControlError(RuntimeError):
    """driver 控制面调用失败。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ControlTimeoutError(ControlError):
    """请求在给定时限内没有完成。"""


class ProtocolVersionError(ControlError):
    """driver 不认识本客户端的协议版本。"""


class UnknownTurnError(ControlError):
    """driver 的 registry 里没有这个 turn_id（进程重启后属正常情形）。"""


@dataclass(frozen=True)
class TurnSubmission:
    """POST /v1/turn 的响应。"""

    turn_id: str
    state: str            # "accepted" | "duplicate"
    turn_state: str       # registry 条目现态："queued" | "running" | "done"

    @property
    def is_duplicate(self) -> bool:
        return self.state == STATE_DUPLICATE


@dataclass(frozen=True)
class EventPage:
    """GET /v1/turn/{turn_id}/events 的响应。`next_after` 直接作为下次 cursor。"""

    events: list[DriverEvent]
    next_after: int


@dataclass(frozen=True)
class HealthStatus:
    ok: bool
    protocol_version: str
    uptime_ms: int
    harness_ready: bool


class ControlClient:
    """经 SandboxBackend 的 HTTP 通道与沙箱内 driver 对话。

    参数：
        backend:          注入的沙箱后端（只用到 `request`）。
        handle:           目标沙箱。
        request_timeout:  非长轮询请求的超时（秒）。
        wait_slack:       长轮询请求在 wait_ms 之上额外允许的传输时间（秒）。
    """

    def __init__(
        self,
        backend: SandboxBackend,
        handle: SandboxHandle,
        *,
        request_timeout: float = 10.0,
        wait_slack: float = 5.0,
    ) -> None:
        if request_timeout <= 0:
            raise ValueError("request_timeout 必须 > 0")
        if wait_slack < 0:
            raise ValueError("wait_slack 必须 >= 0")
        self._backend = backend
        self._handle = handle
        self._request_timeout = request_timeout
        self._wait_slack = wait_slack

    # ---- 端点 ---------------------------------------------------------------

    async def submit_turn(self, turn: TurnEnvelope) -> TurnSubmission:
        """提交一个 turn。重复提交返回 `state="duplicate"` 与条目现态，driver 不重跑。"""
        status, body = await self._request(
            "POST", TURN_ENDPOINT, body=encode_turn_bytes(turn)
        )
        obj = self._ok_json(status, body, path=TURN_ENDPOINT)
        state = _require_str(obj, "state")
        if state not in (STATE_ACCEPTED, STATE_DUPLICATE):
            raise ProtocolError(f"未知的 state {state!r}")
        return TurnSubmission(
            turn_id=_require_str(obj, "turn_id"),
            state=state,
            turn_state=_require_str(obj, "turn_state"),
        )

    async def fetch_events(
        self, turn_id: str, *, after: int = 0, wait_ms: int = DEFAULT_WAIT_MS
    ) -> EventPage:
        """长轮询拉取 seq > after 的事件。重复用同一 cursor 读是幂等的。"""
        if after < 0:
            raise ValueError("after 必须 >= 0")
        if wait_ms < 0:
            raise ValueError("wait_ms 必须 >= 0")
        wait_ms = min(wait_ms, MAX_WAIT_MS)
        query = urlencode({"after": after, "wait_ms": wait_ms})
        path = f"{TURN_ENDPOINT}/{quote(turn_id, safe='')}/events?{query}"

        status, body = await self._request(
            "GET", path, timeout_seconds=wait_ms / 1000 + self._wait_slack
        )
        if status == 404:
            raise UnknownTurnError(f"driver 不认识 turn {turn_id!r}", status=404)
        obj = self._ok_json(status, body, path=path)

        raw_events = obj.get("events")
        if not isinstance(raw_events, list):
            raise ProtocolError("字段 'events' 必须是 array")
        events = [decode_event(item) for item in raw_events]

        next_after = obj.get("next_after")
        if isinstance(next_after, bool) or not isinstance(next_after, int):
            raise ProtocolError("字段 'next_after' 必须是 integer")
        return EventPage(events=events, next_after=next_after)

    async def health(self) -> HealthStatus:
        status, body = await self._request("GET", HEALTH_ENDPOINT)
        obj = self._ok_json(status, body, path=HEALTH_ENDPOINT)
        return HealthStatus(
            ok=bool(obj.get("ok", False)),
            protocol_version=_require_str(obj, "protocol_version"),
            uptime_ms=int(obj.get("uptime_ms", 0)),
            harness_ready=bool(obj.get("harness_ready", False)),
        )

    # ---- workspace（M4 持久化；附录 G） --------------------------------------

    async def get_workspace(self, *, timeout_seconds: float | None = None) -> bytes:
        """取回工作区目录的 tar.gz 字节。空工作区返回一个合法的空归档。

        刻意返回原始字节而不做任何解释：宿主只把它交给 SnapshotStore，归档的结构
        是沙箱侧与沙箱侧之间的约定（driver/workspace.py），host 层不参与。
        """
        status, body = await self._request(
            "GET", WORKSPACE_ENDPOINT, timeout_seconds=timeout_seconds
        )
        if status != 200:
            raise ControlError(
                f"{WORKSPACE_ENDPOINT} 返回 {status}"
                + (f"（{code}）" if (code := _error_code(body)) else ""),
                status=status,
            )
        return body

    async def put_workspace(
        self, data: bytes, *, timeout_seconds: float | None = None
    ) -> None:
        """把 tar.gz 字节解包覆盖进沙箱的工作区目录。"""
        status, body = await self._request(
            "PUT",
            WORKSPACE_ENDPOINT,
            body=data,
            content_type=WORKSPACE_CONTENT_TYPE,
            timeout_seconds=timeout_seconds,
        )
        if status != 200:
            raise ControlError(
                f"{WORKSPACE_ENDPOINT} 返回 {status}"
                + (f"（{code}）" if (code := _error_code(body)) else ""),
                status=status,
            )

    # ---- 传输 ---------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str = "application/json",
        timeout_seconds: float | None = None,
    ) -> tuple[int, bytes]:
        headers = {HEADER_PROTOCOL_VERSION: PROTOCOL_VERSION}
        if body is not None:
            headers["Content-Type"] = content_type
        try:
            return await self._backend.request(
                self._handle,
                method,
                path,
                body=body,
                headers=headers,
                timeout_seconds=(
                    self._request_timeout if timeout_seconds is None else timeout_seconds
                ),
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise ControlTimeoutError(f"{method} {path} 超时") from exc

    def _ok_json(self, status: int, body: bytes, *, path: str) -> dict[str, Any]:
        """把非 200 响应翻译成异常；200 则解析 JSON object。"""
        if status != 200:
            error = _error_code(body)
            if status == 400 and error == "unsupported_protocol_version":
                raise ProtocolVersionError(
                    f"driver 不接受协议版本 {PROTOCOL_VERSION}", status=status
                )
            raise ControlError(
                f"{path} 返回 {status}" + (f"（{error}）" if error else ""),
                status=status,
            )
        obj = decode_json(body)
        if not isinstance(obj, dict):
            raise ProtocolError("响应顶层必须是 JSON object")
        return obj


def _require_str(obj: dict[str, Any], name: str) -> str:
    value = obj.get(name)
    if not isinstance(value, str):
        raise ProtocolError(f"响应字段 {name!r} 必须是 string")
    return value


def _error_code(body: bytes) -> str | None:
    """尽力从错误响应里取 `error` 码；取不到就算了（错误路径不该再抛第二个错）。"""
    try:
        obj = decode_json(body)
    except ProtocolError:
        return None
    if isinstance(obj, dict) and isinstance(obj.get("error"), str):
        return obj["error"]
    return None
