"""到沙箱控制端口的 HTTP request 通道。

stdlib urllib 的阻塞 IO 用线程 executor 包装成 async（零运行时依赖是硬约束）。
本模块不认识沙箱：调用方给它一个已发布的宿主端口（`request_loopback`，docker 版），
或一个完整的 origin（`request_url`，E2B 一类自带 HTTPS 端口代理的 backend）。
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request

from .errors import SandboxTimeoutError

__all__ = ["request_loopback", "request_url"]

_LOOPBACK = "127.0.0.1"


def _perform(
    origin: str,
    method: str,
    path: str,
    body: bytes | None,
    headers: dict[str, str],
    timeout_seconds: float | None,
) -> tuple[int, bytes]:
    if not path.startswith("/"):
        path = "/" + path
    request = urllib.request.Request(  # noqa: S310 - scheme 由调用方构造，非用户输入
        f"{origin.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        # 非 2xx 是协议的合法回答（400 / 404 / 501），交回调用方判读。
        with exc:
            return exc.code, exc.read()
    except TimeoutError as exc:
        raise SandboxTimeoutError(
            f"{method} {path} exceeded timeout of {timeout_seconds}s"
        ) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise SandboxTimeoutError(
                f"{method} {path} exceeded timeout of {timeout_seconds}s"
            ) from exc
        raise


async def request_url(
    origin: str,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: float | None = None,
) -> tuple[int, bytes]:
    """对 `origin`（如 `https://8787-<id>.e2b.app`）发一次 HTTP 请求，返回 (status, body)。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        _perform,
        origin,
        method,
        path,
        body,
        dict(headers or {}),
        timeout_seconds,
    )


async def request_loopback(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: float | None = None,
) -> tuple[int, bytes]:
    """对 127.0.0.1:port 发一次 HTTP 请求，返回 (status, body)。"""
    return await request_url(
        f"http://{_LOOPBACK}:{port}",
        method,
        path,
        body=body,
        headers=headers,
        timeout_seconds=timeout_seconds,
    )
