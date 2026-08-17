"""S3SnapshotStore 行为测试，跑在一个标准库假 S3 server 上。

**不访问真实云**（CONTRACTS.md 附录 C）：假 server 只做三件事——存对象、按
path-style 路径取对象、把收到的请求原样记录下来供断言。它还会用同一套 SigV4
纯函数**基于自己收到的请求行与 header 重算签名**：这不是自证，而是抓"签的东西
和发的东西不一致"这一类真实 bug（canonical_uri 与实际 path 漂移、Host 少了端口、
载荷哈希与 body 不符），线上表现是清一色 403，现场无法反推。
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import pytest

from roost.snapshot import S3Error, S3SnapshotStore
from roost.snapshot import sigv4

ACCESS_KEY = "AKIDEXAMPLE"
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
REGION = "us-east-1"
BUCKET = "roost-snapshots"


class FakeS3State:
    def __init__(self) -> None:
        self.endpoint = ""  # fake_s3 fixture 起服务后填入
        self.objects: dict[str, bytes] = {}
        self.requests: list[dict[str, Any]] = []
        self.force_status: int | None = None


def _make_handler(state: FakeS3State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # 测试里不要往 stderr 喷日志
            pass

        def _record(self, body: bytes) -> None:
            state.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                }
            )

        def _respond(self, status: int, body: bytes = b"") -> None:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_PUT(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 约定)
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            self._record(body)
            if state.force_status is not None:
                self._respond(state.force_status, b"<Error>forced</Error>")
                return
            state.objects[self.path] = body
            self._respond(200)

        def do_GET(self) -> None:  # noqa: N802
            self._record(b"")
            if state.force_status is not None:
                self._respond(state.force_status, b"<Error>forced</Error>")
                return
            body = state.objects.get(self.path)
            if body is None:
                self._respond(404, b"<Error>NoSuchKey</Error>")
                return
            self._respond(200, body)

    return Handler


@pytest.fixture
def fake_s3():
    state = FakeS3State()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    state.endpoint = f"http://{host}:{port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def make_store(fake_s3: FakeS3State, *, prefix: str = "") -> S3SnapshotStore:
    return S3SnapshotStore(
        BUCKET,
        endpoint_url=fake_s3.endpoint,
        region=REGION,
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        prefix=prefix,
        timeout_seconds=10.0,
    )


def parse_authorization(header: str) -> dict[str, str]:
    algorithm, _, rest = header.partition(" ")
    parts = dict(
        field.strip().split("=", 1) for field in rest.split(",") if field.strip()
    )
    parts["algorithm"] = algorithm
    return parts


def resign(request: dict[str, Any]) -> str:
    """用收到的请求重算 Authorization：签的内容必须与发的内容一致。"""
    headers = {
        name: value
        for name, value in request["headers"].items()
        if name == "host" or name.startswith("x-amz-")
    }
    signed_headers = parse_authorization(request["headers"]["authorization"])[
        "SignedHeaders"
    ].split(";")
    headers = {name: headers[name] for name in signed_headers}
    canonical, _ = sigv4.canonical_request(
        method=request["method"],
        canonical_uri=urlsplit(request["path"]).path,
        canonical_query="",
        headers=headers,
        payload_hash=headers["x-amz-content-sha256"],
    )
    amz_date = headers["x-amz-date"]
    scope = sigv4.credential_scope(
        date_stamp=amz_date[:8], region=REGION, service="s3"
    )
    return sigv4.calculate_signature(
        secret_key=SECRET_KEY,
        date_stamp=amz_date[:8],
        region=REGION,
        service="s3",
        string_to_sign=sigv4.string_to_sign(
            amz_date=amz_date, scope=scope, canonical_request=canonical
        ),
    )


async def test_put_then_get_roundtrip(fake_s3: FakeS3State) -> None:
    store = make_store(fake_s3)
    payload = b"\x00binary\xffsnapshot"

    await store.put("session-1", payload)
    assert await store.get("session-1") == payload

    put_request, get_request = fake_s3.requests
    assert put_request["method"] == "PUT"
    assert put_request["body"] == payload
    assert get_request["method"] == "GET"
    # path-style：/{bucket}/{key}
    assert put_request["path"] == f"/{BUCKET}/session-1"
    assert get_request["path"] == put_request["path"]


async def test_prefix_is_a_literal_key_prefix(fake_s3: FakeS3State) -> None:
    store = make_store(fake_s3, prefix="snapshots/tenant-a/")
    await store.put("session-1", b"payload")
    assert await store.get("session-1") == b"payload"
    assert (
        fake_s3.requests[0]["path"] == f"/{BUCKET}/snapshots/tenant-a/session-1"
    )


async def test_key_is_uri_encoded_but_slashes_are_kept(fake_s3: FakeS3State) -> None:
    store = make_store(fake_s3)
    await store.put("a b/会话?x", b"payload")
    assert (
        fake_s3.requests[0]["path"]
        == f"/{BUCKET}/a%20b/%E4%BC%9A%E8%AF%9D%3Fx"
    )
    assert await store.get("a b/会话?x") == b"payload"


async def test_get_missing_object_returns_none(fake_s3: FakeS3State) -> None:
    store = make_store(fake_s3)
    assert await store.get("never-written") is None


async def test_non_404_error_raises(fake_s3: FakeS3State) -> None:
    store = make_store(fake_s3)
    fake_s3.force_status = 500

    with pytest.raises(S3Error) as get_error:
        await store.get("session-1")
    assert get_error.value.status == 500

    with pytest.raises(S3Error) as put_error:
        await store.put("session-1", b"payload")
    assert put_error.value.status == 500
    assert b"forced" in put_error.value.body


async def test_unreachable_endpoint_raises_s3_error() -> None:
    store = S3SnapshotStore(
        BUCKET,
        endpoint_url="http://127.0.0.1:1",
        region=REGION,
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        timeout_seconds=2.0,
    )
    with pytest.raises(S3Error):
        await store.get("session-1")


async def test_authorization_header_structure(fake_s3: FakeS3State) -> None:
    store = make_store(fake_s3)
    payload = b"snapshot-bytes"
    await store.put("session-1", payload)

    request = fake_s3.requests[0]
    parts = parse_authorization(request["headers"]["authorization"])
    assert parts["algorithm"] == "AWS4-HMAC-SHA256"

    amz_date = request["headers"]["x-amz-date"]
    assert parts["Credential"] == (
        f"{ACCESS_KEY}/{amz_date[:8]}/{REGION}/s3/aws4_request"
    )
    assert parts["SignedHeaders"] == "host;x-amz-content-sha256;x-amz-date"
    assert len(parts["Signature"]) == 64
    assert set(parts["Signature"]) <= set("0123456789abcdef")

    # 载荷哈希覆盖真实 body；Host 与实际连接的 host:port 一致。
    assert request["headers"]["x-amz-content-sha256"] == sigv4.sha256_hex(payload)
    endpoint_host = urlsplit(fake_s3.endpoint).netloc
    assert request["headers"]["host"] == endpoint_host


async def test_signature_matches_the_request_as_actually_sent(
    fake_s3: FakeS3State,
) -> None:
    store = make_store(fake_s3, prefix="snapshots/")
    await store.put("a b/session-1", b"payload")
    await store.get("a b/session-1")

    for request in fake_s3.requests:
        expected = parse_authorization(request["headers"]["authorization"])["Signature"]
        assert resign(request) == expected, request["method"]


async def test_get_signs_empty_payload_hash(fake_s3: FakeS3State) -> None:
    store = make_store(fake_s3)
    await store.get("session-1")
    assert (
        fake_s3.requests[0]["headers"]["x-amz-content-sha256"]
        == sigv4.EMPTY_PAYLOAD_SHA256
    )


def test_invalid_endpoint_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        S3SnapshotStore(
            BUCKET,
            endpoint_url="not-a-url",
            access_key=ACCESS_KEY,
            secret_key=SECRET_KEY,
        )


async def test_empty_key_is_rejected(fake_s3: FakeS3State) -> None:
    store = make_store(fake_s3)
    with pytest.raises(ValueError):
        await store.put("", b"payload")
