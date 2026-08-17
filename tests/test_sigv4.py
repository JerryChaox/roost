"""SigV4 known-answer 测试。

向量逐字取自 AWS 官方 SigV4 测试套件 `aws-sig-v4-test-suite`（AWS 文档随附，
公开镜像见 awslabs/aws-c-auth `tests/aws-signing-test-suite/v4`）。用例名与套件
目录名一一对应，便于回溯。凭据是 AWS 文档中的示例值，不是真实凭据。

这些向量保护的回归很具体：canonical 字符串拼装、header 归一化与排序、
UriEncode 的字节级行为、签名密钥逐级派生——任何一处偏差都会让线上请求 403，
而 403 的现场无法反推是哪一步错了。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from roost.snapshot import sigv4

ACCESS_KEY = "AKIDEXAMPLE"
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
REGION = "us-east-1"
SERVICE = "service"
MOMENT = datetime(2015, 8, 30, 12, 36, 0, tzinfo=timezone.utc)
AMZ_DATE = "20150830T123600Z"
DATE_STAMP = "20150830"
SCOPE = "20150830/us-east-1/service/aws4_request"
HOST = "example.amazonaws.com"


def _signature(request: str) -> str:
    return sigv4.calculate_signature(
        secret_key=SECRET_KEY,
        date_stamp=DATE_STAMP,
        region=REGION,
        service=SERVICE,
        string_to_sign=sigv4.string_to_sign(
            amz_date=AMZ_DATE, scope=SCOPE, canonical_request=request
        ),
    )


def test_format_timestamp_matches_suite_stamps() -> None:
    assert sigv4.format_timestamp(MOMENT) == (AMZ_DATE, DATE_STAMP)


def test_format_timestamp_converts_to_utc() -> None:
    naive = datetime(2015, 8, 30, 12, 36, 0)
    assert sigv4.format_timestamp(naive) == (AMZ_DATE, DATE_STAMP)


def test_get_vanilla_vector() -> None:
    """套件用例 get-vanilla：最小 GET，只签 host 与 x-amz-date。"""
    request, signed_headers = sigv4.canonical_request(
        method="GET",
        canonical_uri="/",
        canonical_query="",
        headers={"Host": HOST, "X-Amz-Date": AMZ_DATE},
        payload_hash=sigv4.EMPTY_PAYLOAD_SHA256,
    )

    assert request == (
        "GET\n"
        "/\n"
        "\n"
        f"host:{HOST}\n"
        f"x-amz-date:{AMZ_DATE}\n"
        "\n"
        "host;x-amz-date\n"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert signed_headers == "host;x-amz-date"
    assert sigv4.string_to_sign(
        amz_date=AMZ_DATE, scope=SCOPE, canonical_request=request
    ) == (
        "AWS4-HMAC-SHA256\n"
        f"{AMZ_DATE}\n"
        f"{SCOPE}\n"
        "bb579772317eb040ac9ed261061d46c1f17a8133879d6129b6e1c25292927e63"
    )
    assert (
        _signature(request)
        == "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


@pytest.mark.parametrize(
    ("case", "canonical_uri", "expected_signature"),
    [
        (
            "get-unreserved",
            "/-._~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "abcdefghijklmnopqrstuvwxyz",
            "07ef7494c76fa4850883e2b006601f940f8a34d404d0cfa977f52a65bbf5f24f",
        ),
        (
            "get-utf8",
            "/%E1%88%B4",
            "8318018e0b0f223aa2bbf98705b62bb787dc9c0e678f255a891fd03141be5d85",
        ),
        (
            "get-space-normalized",
            "/example%20space/",
            "652487583200325589f1fba4c7e578f72c47cb61beeca81406b39ddec1366741",
        ),
    ],
)
def test_path_vectors(case: str, canonical_uri: str, expected_signature: str) -> None:
    """套件的 path 编码用例：签名覆盖的是**已编码**路径，不再二次编码。"""
    request, _ = sigv4.canonical_request(
        method="GET",
        canonical_uri=canonical_uri,
        canonical_query="",
        headers={"Host": HOST, "X-Amz-Date": AMZ_DATE},
        payload_hash=sigv4.EMPTY_PAYLOAD_SHA256,
    )
    assert _signature(request) == expected_signature, case


def test_uri_encode_matches_suite_paths() -> None:
    unreserved = "-._~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    assert sigv4.uri_encode(unreserved) == unreserved
    assert sigv4.uri_encode("ሴ") == "%E1%88%B4"
    assert sigv4.uri_encode("example space/") == "example%20space%2F"
    assert sigv4.uri_encode("example space/", encode_slash=False) == "example%20space/"
    # '+' 必须编码成 %2B（不是保留为 '+'），'=' 与 ':' 同样要编码。
    assert sigv4.uri_encode("a+b=c:d") == "a%2Bb%3Dc%3Ad"


def test_post_x_www_form_urlencoded_vector_via_sign_request() -> None:
    """套件用例 post-x-www-form-urlencoded：带载荷、带额外签名 header 的完整签名。

    这条向量把 `sign_request` 端到端钉死——它自己补 x-amz-date /
    x-amz-content-sha256，并产出与官方一致的 Authorization。
    """
    body = b"Param1=value1"
    payload_hash = sigv4.sha256_hex(body)
    assert payload_hash == (
        "9095672bbd1f56dfc5b65f3e153adc8731a4a654192329106275f4c7b24d0b6e"
    )

    headers = sigv4.sign_request(
        method="POST",
        canonical_uri="/",
        headers={
            "Host": HOST,
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body)),
        },
        payload_hash=payload_hash,
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        region=REGION,
        service=SERVICE,
        moment=MOMENT,
    )

    assert headers["x-amz-date"] == AMZ_DATE
    assert headers["x-amz-content-sha256"] == payload_hash
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 "
        f"Credential={ACCESS_KEY}/{SCOPE}, "
        "SignedHeaders=content-length;content-type;host;"
        "x-amz-content-sha256;x-amz-date, "
        "Signature=d3875051da38690788ef43de4db0d8f280229d82040bfac253562e56c3f20e0b"
    )


def test_header_values_are_trimmed_and_collapsed() -> None:
    """套件用例 get-header-value-trim 的语义：首尾空白去掉、内部连续空白折叠。"""
    canonical, signed = sigv4.canonical_headers(
        {"My-Header1": "  a   b   c  ", "Host": HOST}
    )
    assert canonical == f"host:{HOST}\nmy-header1:a b c\n"
    assert signed == "host;my-header1"


def test_canonical_query_string_sorts_after_encoding() -> None:
    assert sigv4.canonical_query_string({}) == ""
    assert (
        sigv4.canonical_query_string({"b": "2", "a": "1", "A": "0"}) == "A=0&a=1&b=2"
    )
    assert sigv4.canonical_query_string({"k": "a b"}) == "k=a%20b"


def test_signing_key_is_derived_per_date_region_service() -> None:
    """派生链换任一段都必须产出不同密钥——错用共享密钥会静默签出错误签名。"""
    base = dict(secret_key=SECRET_KEY, date_stamp=DATE_STAMP, region=REGION)
    key = sigv4.signing_key(service=SERVICE, **base)
    assert len(key) == 32
    assert key != sigv4.signing_key(service="s3", **base)
    assert key != sigv4.signing_key(
        secret_key=SECRET_KEY, date_stamp="20150831", region=REGION, service=SERVICE
    )
    assert key != sigv4.signing_key(
        secret_key=SECRET_KEY,
        date_stamp=DATE_STAMP,
        region="us-west-2",
        service=SERVICE,
    )
