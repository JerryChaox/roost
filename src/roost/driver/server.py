"""HTTP 路由与编解码边界。

契约见 CONTRACTS.md《附录 B — 端点行为》。本模块只做三件事：

1. 校验协议版本 header（不识别 -> 400 `unsupported_protocol_version`）；
2. 把路径映射到四个端点；
3. 在 wire（bytes/JSON）与库内类型之间翻译，经 `control/envelope.py` ——
   **双端同一份编解码实现**。

一切"发生了什么"都不在这里：条目状态在 registry.py，执行在 worker.py，
事件序与长轮询在 emit.py，HTTP 报文在 httpd.py。因此本文件的变更原因只有一个：
协议端点面变化。
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import unquote

from ..control.envelope import (
    ProtocolError,
    decode_turn_bytes,
    encode_event,
    encode_json,
)
from ..protocol import (
    HEADER_PROTOCOL_VERSION,
    HEALTH_ENDPOINT,
    PROTOCOL_VERSION,
    TURN_ENDPOINT,
    UPDATE_ENDPOINT,
)
from .emit import EventLog
from .harness import Harness
from .httpd import HttpServer, Request, Response
from .registry import TurnRegistry
from .worker import TurnWorker

__all__ = ["ControlServer", "DEFAULT_WAIT_MS", "MAX_WAIT_MS"]

DEFAULT_WAIT_MS = 10_000
MAX_WAIT_MS = 30_000

_EVENTS_SUFFIX = "/events"
_TURN_PREFIX = TURN_ENDPOINT + "/"


class ControlServer:
    """沙箱内 loopback 控制面。"""

    def __init__(
        self,
        harness: Harness,
        *,
        host: str = "127.0.0.1",
        port: int = 8787,
    ) -> None:
        self._registry = TurnRegistry()
        self._events = EventLog()
        self._worker = TurnWorker(harness, self._registry, self._events)
        self._http = HttpServer(self._handle, host=host, port=port)
        self._started_at = time.monotonic()

    @property
    def port(self) -> int:
        return self._http.port

    async def start(self) -> None:
        self._started_at = time.monotonic()
        await self._worker.start()
        await self._http.start()

    async def serve_forever(self) -> None:
        await self._http.serve_forever()

    async def close(self) -> None:
        await self._http.close()
        await self._worker.stop()

    # ---- 路由 ---------------------------------------------------------------

    async def _handle(self, request: Request) -> Response:
        if request.headers.get(HEADER_PROTOCOL_VERSION.lower()) != PROTOCOL_VERSION:
            return _json(400, {"error": "unsupported_protocol_version"})

        path = request.path
        if path == TURN_ENDPOINT:
            return self._route(request, "POST", self._submit_turn)
        if path == HEALTH_ENDPOINT:
            return self._route(request, "GET", self._health)
        if path == UPDATE_ENDPOINT:
            return self._route(request, "POST", self._update)
        if path.startswith(_TURN_PREFIX) and path.endswith(_EVENTS_SUFFIX):
            if request.method != "GET":
                return _json(405, {"error": "method_not_allowed"})
            turn_id = unquote(path[len(_TURN_PREFIX) : -len(_EVENTS_SUFFIX)])
            return await self._read_events(request, turn_id)
        return _json(404, {"error": "not_found"})

    def _route(self, request: Request, method: str, handler: Any) -> Response:
        if request.method != method:
            return _json(405, {"error": "method_not_allowed"})
        return handler(request)

    # ---- 端点实现（只做翻译；语义在各自的模块里） ------------------------------

    def _submit_turn(self, request: Request) -> Response:
        try:
            turn = decode_turn_bytes(request.body)
        except ProtocolError:
            return _json(400, {"error": "invalid_body"})

        result = self._registry.submit(turn)
        if result.accepted:
            self._worker.submit(turn)
        return _json(
            200,
            {
                "turn_id": result.entry.turn_id,
                "state": "accepted" if result.accepted else "duplicate",
                "turn_state": result.entry.state,
            },
        )

    async def _read_events(self, request: Request, turn_id: str) -> Response:
        if self._registry.get(turn_id) is None:
            return _json(404, {"error": "unknown_turn"})
        try:
            after = _query_int(request, "after", default=0, minimum=0)
            wait_ms = _query_int(
                request, "wait_ms", default=DEFAULT_WAIT_MS, minimum=0
            )
        except ValueError:
            return _json(400, {"error": "invalid_query"})

        events, next_after = await self._events.read(
            turn_id, after=after, wait_ms=min(wait_ms, MAX_WAIT_MS)
        )
        return _json(
            200,
            {
                "events": [encode_event(event) for event in events],
                "next_after": next_after,
            },
        )

    def _health(self, _request: Request) -> Response:
        return _json(
            200,
            {
                "ok": True,
                "protocol_version": PROTOCOL_VERSION,
                "uptime_ms": int((time.monotonic() - self._started_at) * 1000),
                "harness_ready": self._worker.ready,
            },
        )

    def _update(self, _request: Request) -> Response:
        # M2 只保留协议位；行为归 M6（零停机 forced update）。
        return _json(501, {"error": "reserved_until_m6"})


def _json(status: int, body: dict[str, Any]) -> Response:
    return Response(
        status=status,
        body=encode_json(body),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            HEADER_PROTOCOL_VERSION: PROTOCOL_VERSION,
        },
    )


def _query_int(request: Request, name: str, *, default: int, minimum: int) -> int:
    raw = request.query.get(name)
    if raw is None or raw == "":
        return default
    value = int(raw)          # 非法值抛 ValueError，由调用方翻译成 400
    if value < minimum:
        raise ValueError(f"{name} 必须 >= {minimum}")
    return value
