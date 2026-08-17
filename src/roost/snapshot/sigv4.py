"""AWS Signature Version 4 —— 纯函数实现，只用标准库 hmac/hashlib。

存在的唯一理由：`s3.py` 要对 S3 兼容端点发签名请求，而**零运行时依赖是硬约束**
（CONTRACTS.md 附录 C），不能引 boto3/botocore。

本模块只承担一类职责：把请求要素翻译成 SigV4 规定的字符串并算出签名。它不发
任何 IO、不认识 S3、不持有凭据状态——全部输入显式传入，包括时间戳，因此每个
函数都是可用 known-answer 向量钉死的纯函数（见 tests/test_sigv4.py，向量取自
AWS 官方 SigV4 测试套件 aws-sig-v4-test-suite）。

不实现的部分（当前用不到，不预先承诺）：query-string（presigned）签名、
SigV4a、chunked/streaming 载荷签名、URI path 规范化（调用方给出的
canonical_uri 必须已是编码后的绝对路径）。
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone

__all__ = [
    "ALGORITHM",
    "EMPTY_PAYLOAD_SHA256",
    "UNSIGNED_PAYLOAD",
    "authorization_header",
    "calculate_signature",
    "canonical_headers",
    "canonical_query_string",
    "canonical_request",
    "credential_scope",
    "format_timestamp",
    "sha256_hex",
    "sign_request",
    "signing_key",
    "string_to_sign",
    "uri_encode",
]

ALGORITHM = "AWS4-HMAC-SHA256"
TERMINATOR = "aws4_request"

#: 空载荷的 SHA256（GET/HEAD 用）。
EMPTY_PAYLOAD_SHA256 = hashlib.sha256(b"").hexdigest()

#: S3 允许用它替代真实载荷哈希；本库不用，留给宿主扩展。
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"

# AWS UriEncode 的 unreserved 集合：字母、数字、'-'、'.'、'_'、'~'。
# 刻意不用 urllib.parse.quote：它的 safe 集合随版本与平台有历史差异，而签名
# 对单个字节的编码差异零容忍——这里按字节自己实现，行为完全确定。
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ" "abcdefghijklmnopqrstuvwxyz" "0123456789" "-._~"
)


def sha256_hex(data: bytes) -> str:
    """载荷哈希：小写十六进制。"""
    return hashlib.sha256(data).hexdigest()


def uri_encode(value: str, *, encode_slash: bool = True) -> str:
    """AWS UriEncode：unreserved 字符原样，其余按 UTF-8 字节转大写 %XX。

    `encode_slash=False` 用于对象 key —— S3 规定 key 名里的 '/' 不编码。
    """
    out: list[str] = []
    for byte in value.encode("utf-8"):
        char = chr(byte)
        if char in _UNRESERVED:
            out.append(char)
        elif char == "/" and not encode_slash:
            out.append("/")
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def canonical_query_string(params: Mapping[str, str] | Iterable[tuple[str, str]]) -> str:
    """编码每个 name/value，再按编码后的结果排序。空参数返回空串。"""
    items = list(params.items()) if isinstance(params, Mapping) else list(params)
    encoded = sorted((uri_encode(name), uri_encode(value)) for name, value in items)
    return "&".join(f"{name}={value}" for name, value in encoded)


def _normalize_header_value(value: str) -> str:
    """去首尾空白，并把连续空白折叠成单个空格（SigV4 的 Trim 语义）。"""
    return " ".join(value.split())


def canonical_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    """返回 (canonical_headers, signed_headers)。

    名字小写、按名字排序，每行以 '\\n' 结尾；signed_headers 是分号连接的同一批名字。
    """
    normalized = {
        name.lower().strip(): _normalize_header_value(value)
        for name, value in headers.items()
    }
    names = sorted(normalized)
    canonical = "".join(f"{name}:{normalized[name]}\n" for name in names)
    return canonical, ";".join(names)


def canonical_request(
    *,
    method: str,
    canonical_uri: str,
    canonical_query: str,
    headers: Mapping[str, str],
    payload_hash: str,
) -> tuple[str, str]:
    """返回 (canonical_request, signed_headers)。

    `canonical_uri` 必须已经是编码后的绝对路径（空路径用 '/'）；本函数不做
    path 规范化，也不再次编码——重复编码是 SigV4 最常见的错法之一。
    """
    canonical_header_block, signed_headers = canonical_headers(headers)
    request = "\n".join(
        (
            method.upper(),
            canonical_uri or "/",
            canonical_query,
            canonical_header_block,
            signed_headers,
            payload_hash,
        )
    )
    return request, signed_headers


def credential_scope(*, date_stamp: str, region: str, service: str) -> str:
    return f"{date_stamp}/{region}/{service}/{TERMINATOR}"


def string_to_sign(*, amz_date: str, scope: str, canonical_request: str) -> str:
    return "\n".join(
        (ALGORITHM, amz_date, scope, sha256_hex(canonical_request.encode("utf-8")))
    )


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signing_key(
    *, secret_key: str, date_stamp: str, region: str, service: str
) -> bytes:
    """逐级派生 kDate → kRegion → kService → kSigning。"""
    date_key = _hmac(f"AWS4{secret_key}".encode("utf-8"), date_stamp)
    region_key = _hmac(date_key, region)
    service_key = _hmac(region_key, service)
    return _hmac(service_key, TERMINATOR)


def calculate_signature(
    *,
    secret_key: str,
    date_stamp: str,
    region: str,
    service: str,
    string_to_sign: str,
) -> str:
    key = signing_key(
        secret_key=secret_key, date_stamp=date_stamp, region=region, service=service
    )
    return hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()


def authorization_header(
    *, access_key: str, scope: str, signed_headers: str, signature: str
) -> str:
    return (
        f"{ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def format_timestamp(moment: datetime) -> tuple[str, str]:
    """datetime → (amz_date, date_stamp)，例如 ('20150830T123600Z', '20150830')。

    naive datetime 按 UTC 解释；带时区的先转 UTC。
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%SZ"), moment.strftime("%Y%m%d")


def sign_request(
    *,
    method: str,
    canonical_uri: str,
    headers: Mapping[str, str],
    payload_hash: str,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
    moment: datetime,
    query: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
) -> dict[str, str]:
    """签一个 header-based 请求，返回**待发送的完整 header 字典**。

    在传入 headers 之上补 `x-amz-date`、`x-amz-content-sha256` 与 `Authorization`；
    传入的 headers 必须已含 `Host`（含端口，若非默认端口），因为签名覆盖它。
    时间戳由调用方给出（`moment`），保持本函数可测且无隐藏状态。
    """
    amz_date, date_stamp = format_timestamp(moment)
    signed = dict(headers)
    signed["x-amz-date"] = amz_date
    signed["x-amz-content-sha256"] = payload_hash

    request, signed_headers = canonical_request(
        method=method,
        canonical_uri=canonical_uri,
        canonical_query=canonical_query_string(query or ()),
        headers=signed,
        payload_hash=payload_hash,
    )
    scope = credential_scope(date_stamp=date_stamp, region=region, service=service)
    signature = calculate_signature(
        secret_key=secret_key,
        date_stamp=date_stamp,
        region=region,
        service=service,
        string_to_sign=string_to_sign(
            amz_date=amz_date, scope=scope, canonical_request=request
        ),
    )
    signed["Authorization"] = authorization_header(
        access_key=access_key,
        scope=scope,
        signed_headers=signed_headers,
        signature=signature,
    )
    return signed
