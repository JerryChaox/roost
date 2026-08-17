"""到已发布宿主端口的 HTTP request 通道。

stdlib urllib 的阻塞 IO 用线程 executor 包装成 async（零运行时依赖是硬约束）。
本模块不认识容器：调用方给它一个 127.0.0.1 上的端口即可。
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request

from .errors import SandboxTimeoutError

__all__ = ["request_loopback"]

_LOOPBACK = "127.0.0.1"


def _perform(
    port: int,
    method: str,
    path: str,
    body: bytes | None,
    headers: dict[str, str],
    timeout_seconds: float | None,
) -> tuple[int, bytes]:
    if not path.startswith("/"):
        path = "/" + path
    request = urllib.request.Request(  # noqa: S310 - loopback http by construction
        f"http://{_LOOPBACK}:{port}{path}",
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
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        _perform,
        port,
        method,
        path,
        body,
        dict(headers or {}),
        timeout_seconds,
    )
