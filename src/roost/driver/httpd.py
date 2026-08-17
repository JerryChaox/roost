"""最小 HTTP/1.1 传输层（asyncio，仅标准库）。

契约见 CONTRACTS.md《附录 B — M2 交付模块》：driver 只允许依赖标准库，HTTP 实现
选型由实现者定，但"路由/编解码/状态机/执行循环"四类职责不得混居。本模块是那条
职责线之下的第五类——**字节与 HTTP 报文**：解析请求行/头/体，写回响应。它不认识
任何 roost 概念（端点、envelope、turn），因此 server.py 可以只谈路由。

只实现控制面用得到的子集：请求行 + 头 + Content-Length 体 + keep-alive；
不支持 chunked 请求体（明确拒绝而不是猜），不做 TLS（loopback 控制面）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

__all__ = ["Request", "Response", "Handler", "HttpServer"]

_MAX_HEADER_BYTES = 64 * 1024
_MAX_BODY_BYTES = 8 * 1024 * 1024

_REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    411: "Length Required",
    413: "Payload Too Large",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
}


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]        # 键统一小写
    body: bytes


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


Handler = Callable[[Request], Awaitable[Response]]


class _BadRequest(Exception):
    """报文层面无法处理，带一个要回给对端的状态码。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class HttpServer:
    """绑定 host:port 的最小 HTTP 服务。

    `start()` 之后 `port` 是**实际**绑定端口（传 0 时由内核分配，供测试取用）。
    """

    def __init__(
        self,
        handler: Handler,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        max_body_bytes: int = _MAX_BODY_BYTES,
    ) -> None:
        self._handler = handler
        self._host = host
        self._port = port
        self._max_body_bytes = max_body_bytes
        self._server: asyncio.Server | None = None
        self._connections: dict[asyncio.Task[None], asyncio.StreamWriter] = {}

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        server = await asyncio.start_server(
            self._handle_client, self._host, self._port, limit=_MAX_HEADER_BYTES
        )
        self._server = server
        self._port = server.sockets[0].getsockname()[1]

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("先调用 start()")
        # 刻意不用 `async with server`：它的 __aexit__ 会 wait_closed()，而
        # wait_closed() 等的是全部在飞连接自然结束——挂着的长轮询会把关停拖到
        # wait_ms 上限。关停清理统一走本类的 close()（取消在飞连接）。
        await self._server.serve_forever()

    async def close(self) -> None:
        """停止监听并**取消在飞连接**。

        取消是必须的而不是礼貌：长轮询连接可以合法地挂在那里等满 30s，只关 writer
        并不会把处理协程叫醒，`Server.wait_closed()` 会一直等它——那会让一次
        SIGTERM 变成半分钟的关停延迟（沙箱替换与 forced update 都踩在这条路上）。
        """
        server, self._server = self._server, None
        connections, self._connections = self._connections, {}
        for task, writer in connections.items():
            task.cancel()
            writer.close()
        for task in connections:
            try:
                await task
            except (asyncio.CancelledError, OSError):
                pass
        if server is not None:
            server.close()
            await server.wait_closed()

    # ---- 连接处理 -----------------------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections[task] = writer
        try:
            while True:
                try:
                    request = await self._read_request(reader)
                except _BadRequest as exc:
                    await self._write(writer, Response(exc.status), close=True)
                    return
                if request is None:      # 对端关闭
                    return

                try:
                    response = await self._handler(request)
                except asyncio.CancelledError:
                    raise
                except Exception:        # noqa: BLE001 —— 处理器异常不得拖垮监听
                    response = Response(500)

                keep_alive = request.headers.get("connection", "").lower() != "close"
                await self._write(writer, response, close=not keep_alive)
                if not keep_alive:
                    return
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            return
        finally:
            if task is not None:
                self._connections.pop(task, None)
            writer.close()

    async def _read_request(self, reader: asyncio.StreamReader) -> Request | None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            return None
        except asyncio.LimitOverrunError as exc:
            raise _BadRequest(431, "请求头过大") from exc

        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split()
        if len(parts) != 3:
            raise _BadRequest(400, "非法请求行")
        method, target, _version = parts

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, sep, value = line.partition(":")
            if not sep:
                raise _BadRequest(400, "非法请求头")
            headers[name.strip().lower()] = value.strip()

        if "transfer-encoding" in headers:
            raise _BadRequest(411, "不支持 chunked 请求体")

        length_raw = headers.get("content-length", "0")
        try:
            length = int(length_raw)
        except ValueError as exc:
            raise _BadRequest(400, "非法 Content-Length") from exc
        if length < 0:
            raise _BadRequest(400, "非法 Content-Length")
        if length > self._max_body_bytes:
            raise _BadRequest(413, "请求体过大")

        try:
            body = await reader.readexactly(length) if length else b""
        except asyncio.IncompleteReadError as exc:
            raise _BadRequest(400, "请求体不完整") from exc

        split = urlsplit(target)
        return Request(
            method=method.upper(),
            path=split.path,
            query=dict(parse_qsl(split.query)),
            headers=headers,
            body=body,
        )

    async def _write(
        self, writer: asyncio.StreamWriter, response: Response, *, close: bool
    ) -> None:
        reason = _REASONS.get(response.status, "")
        head = [f"HTTP/1.1 {response.status} {reason}".rstrip()]
        head.extend(f"{name}: {value}" for name, value in response.headers.items())
        head.append(f"Content-Length: {len(response.body)}")
        head.append("Connection: " + ("close" if close else "keep-alive"))
        writer.write("\r\n".join(head).encode("latin-1") + b"\r\n\r\n" + response.body)
        try:
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            return
