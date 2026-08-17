"""S3SnapshotStore —— SnapshotStore port 的 S3 兼容实现。

契约见 CONTRACTS.md《附录 C：M4 SnapshotStore 实现契约》。本模块只承担一类职责：
把 put/get 翻译成一次签名过的 HTTP 请求。签名逻辑全在 `sigv4.py`（纯函数，
独立可测），本模块只做 URL 组装、IO 与状态码映射。

设计取舍：

- **path-style URL**（`{endpoint}/{bucket}/{key}`）。virtual-hosted style 要求
  bucket 名是 DNS 合法且端点支持通配子域，MinIO / 本地假 server / GCS HMAC
  互操作场景下 path-style 是唯一处处可用的形态。
- **单请求 PUT/GET，不做 multipart**。因此**对象大小上界由宿主负责**：单个
  snapshot 必须小到能一次请求传完（S3 单次 PUT 上限 5 GiB，实际受宿主内存与
  超时约束更早封顶）。需要更大对象时应在宿主侧分片，而不是在这里长出一套
  multipart 状态机。
- **零运行时依赖**：urllib + 标准库 hmac/hashlib，不引 boto3/aiohttp。同步 IO
  用 `asyncio.to_thread` 包成 async。
- `endpoint_url` 可覆盖 → 兼容 MinIO / GCS HMAC 等 S3 兼容端点。

语义：GET 404 → None（未命中，不是错误，恢复路径据此走冷启动）；其余非 2xx
raise `S3Error`。不做重试——重试策略与 backoff 归调用方（I2：snapshot 写失败
不影响 turn 结果）。
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import sigv4

__all__ = ["S3Error", "S3SnapshotStore"]

_SERVICE = "s3"
_DEFAULT_TIMEOUT_SECONDS = 30.0


class S3Error(RuntimeError):
    """非 2xx（且非 GET 404）响应。`status` 为 HTTP 状态码，0 表示传输层失败。"""

    def __init__(self, message: str, *, status: int = 0, body: bytes = b"") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class S3SnapshotStore:
    """把 snapshot 存进 S3 兼容对象存储。

    `prefix` 是**字面前缀**，不代表目录：需要目录语义请自带结尾 '/'
    （例如 `prefix="snapshots/"`）。key 本身 opaque，不被解释。
    """

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region: str = "us-east-1",
        access_key: str,
        secret_key: str,
        prefix: str = "",
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._bucket = bucket
        self._region = region
        self._access_key = access_key
        self._secret_key = secret_key
        self._prefix = prefix
        self._timeout_seconds = timeout_seconds
        endpoint = endpoint_url or f"https://s3.{region}.amazonaws.com"
        self._endpoint = endpoint.rstrip("/")
        parts = urlsplit(self._endpoint)
        if not parts.scheme or not parts.netloc:
            raise ValueError(f"invalid endpoint_url: {endpoint_url!r}")
        # netloc 含端口时 Host 头也含端口，签名必须与实际发送的一致。
        self._host = parts.netloc
        self._base_path = parts.path.rstrip("/")

    def object_key(self, key: str) -> str:
        if not key:
            raise ValueError("snapshot key must not be empty")
        return f"{self._prefix}{key}"

    def _canonical_uri(self, key: str) -> str:
        encoded_bucket = sigv4.uri_encode(self._bucket)
        encoded_key = sigv4.uri_encode(self.object_key(key), encode_slash=False)
        return f"{self._base_path}/{encoded_bucket}/{encoded_key}"

    def _url(self, key: str) -> str:
        parts = urlsplit(self._endpoint)
        return f"{parts.scheme}://{self._host}{self._canonical_uri(key)}"

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._put_sync, key, data)

    async def get(self, key: str) -> bytes | None:
        return await asyncio.to_thread(self._get_sync, key)

    # ---- 同步实现（在线程里跑） ----

    def _signed_headers(
        self, method: str, key: str, payload_hash: str
    ) -> dict[str, str]:
        return sigv4.sign_request(
            method=method,
            canonical_uri=self._canonical_uri(key),
            headers={"Host": self._host},
            payload_hash=payload_hash,
            access_key=self._access_key,
            secret_key=self._secret_key,
            region=self._region,
            service=_SERVICE,
            moment=datetime.now(timezone.utc),
        )

    def _put_sync(self, key: str, data: bytes) -> None:
        headers = self._signed_headers("PUT", key, sigv4.sha256_hex(data))
        request = urllib.request.Request(
            self._url(key), data=data, headers=headers, method="PUT"
        )
        self._send(request, what=f"PUT {key}")

    def _get_sync(self, key: str) -> bytes | None:
        headers = self._signed_headers("GET", key, sigv4.EMPTY_PAYLOAD_SHA256)
        request = urllib.request.Request(self._url(key), headers=headers, method="GET")
        try:
            return self._send(request, what=f"GET {key}")
        except S3Error as error:
            if error.status == 404:
                return None
            raise

    def _send(self, request: urllib.request.Request, *, what: str) -> bytes:
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout_seconds
            ) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            body = error.read()
            raise S3Error(
                f"{what} failed with HTTP {error.code}", status=error.code, body=body
            ) from error
        except urllib.error.URLError as error:
            raise S3Error(f"{what} failed: {error.reason}") from error
