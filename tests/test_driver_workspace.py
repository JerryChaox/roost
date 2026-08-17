"""workspace 打包/解包与两个协议端点。

防的回归（CONTRACTS.md 附录 G 的验收面）：

- 打包/解包是一对：打出来的包解回去内容一致，空目录也是一个合法归档；
- **逃逸拒绝**：`../` 成员、绝对路径成员、符号链接成员一律拒收——这是本模块唯一
  的安全性承诺，恢复路径会把任意来源的字节交给它；
- 端点层的翻译：GET 给 gzip 字节、PUT 覆盖、非法归档 400、方法不对 405。

端点用真实的 `ControlServer` + 真实 HTTP（loopback）跑，而不是直接调私有处理器：
路由、方法门、协议版本头、Content-Type 都在那一层。
"""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

import pytest

from roost.backends.http import request_loopback
from roost.control.client import WORKSPACE_CONTENT_TYPE, WORKSPACE_ENDPOINT
from roost.driver import EchoHarness
from roost.driver.server import ControlServer
from roost.driver.workspace import (
    DEFAULT_WORKSPACE_DIR,
    ENV_WORKSPACE_DIR,
    UnsafeArchiveError,
    pack_directory,
    unpack_into,
    workspace_dir_from_env,
)
from roost.protocol import HEADER_PROTOCOL_VERSION, PROTOCOL_VERSION

from conftest import make_turn

HEADERS = {HEADER_PROTOCOL_VERSION: PROTOCOL_VERSION}


def _archive(members: dict[str, bytes]) -> bytes:
    """手工造一个 tar.gz（成员名原样写入，用于构造非法归档）。"""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


def _symlink_archive(name: str, target: str) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        tar.addfile(info)
    return gzip.compress(raw.getvalue())


# ---- 纯逻辑 ---------------------------------------------------------------


def test_pack_then_unpack_round_trips_tree(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "nested" / "deep").mkdir(parents=True)
    (source / "counter").write_text("3\n", encoding="utf-8")
    (source / "nested" / "notes.md").write_text("hello 世界", encoding="utf-8")
    (source / "nested" / "deep" / "blob.bin").write_bytes(bytes(range(256)))

    target = tmp_path / "dst"
    unpack_into(pack_directory(source), target)

    assert (target / "counter").read_text(encoding="utf-8") == "3\n"
    assert (target / "nested" / "notes.md").read_text(encoding="utf-8") == "hello 世界"
    assert (target / "nested" / "deep" / "blob.bin").read_bytes() == bytes(range(256))


def test_pack_empty_or_missing_directory_yields_valid_empty_archive(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    for root in (empty, tmp_path / "missing"):
        data = pack_directory(root)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            assert tar.getmembers() == []


def test_unpack_overwrites_and_keeps_unrelated_files(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "counter").write_text("2\n", encoding="utf-8")

    target = tmp_path / "dst"
    target.mkdir()
    (target / "counter").write_text("1\n", encoding="utf-8")
    (target / "keep.txt").write_text("untouched", encoding="utf-8")

    unpack_into(pack_directory(source), target)

    assert (target / "counter").read_text(encoding="utf-8") == "2\n"
    assert (target / "keep.txt").read_text(encoding="utf-8") == "untouched"


def test_pack_skips_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "real.txt").write_text("x", encoding="utf-8")
    (source / "link.txt").symlink_to(tmp_path / "outside.txt")

    with tarfile.open(fileobj=io.BytesIO(pack_directory(source)), mode="r:gz") as tar:
        assert [member.name for member in tar.getmembers()] == ["real.txt"]


@pytest.mark.parametrize(
    "name",
    ["../escape.txt", "nested/../../escape.txt", "/etc/passwd", "./../x"],
)
def test_unpack_rejects_escaping_members(tmp_path: Path, name: str) -> None:
    with pytest.raises(UnsafeArchiveError):
        unpack_into(_archive({name: b"pwned"}), tmp_path / "dst")
    assert not (tmp_path / "escape.txt").exists()


def test_unpack_rejects_symlink_members(tmp_path: Path) -> None:
    with pytest.raises(UnsafeArchiveError):
        unpack_into(_symlink_archive("link", "/etc/passwd"), tmp_path / "dst")


def test_unpack_rejects_whole_archive_when_one_member_is_unsafe(
    tmp_path: Path,
) -> None:
    """一个非法成员就整包拒收，绝不留下解了一半的工作区。"""
    target = tmp_path / "dst"
    with pytest.raises(UnsafeArchiveError):
        unpack_into(_archive({"ok.txt": b"fine", "../bad": b"nope"}), target)
    assert not (target / "ok.txt").exists()


def test_unpack_raises_tar_error_on_corrupt_archive(tmp_path: Path) -> None:
    with pytest.raises(tarfile.TarError):
        unpack_into(b"definitely not a gzip", tmp_path / "dst")


def test_workspace_dir_from_env_defaults_and_overrides() -> None:
    assert workspace_dir_from_env({}) == Path(DEFAULT_WORKSPACE_DIR)
    assert workspace_dir_from_env({ENV_WORKSPACE_DIR: "/tmp/ws"}) == Path("/tmp/ws")


# ---- EchoHarness 的 counter（Demo 2 的观测面） ------------------------------


async def test_echo_harness_counter_increments_workspace_file(tmp_path: Path) -> None:
    """counter 的值来自工作区文件——它跨沙箱续增，正是"工作区被恢复了"的证据。"""
    harness = EchoHarness(chunk_size=64, workspace_dir=tmp_path)
    seen: list[str] = []

    async def run(text: str) -> None:
        del seen[:]
        await harness.run(
            make_turn(payload={"text": text, "counter": True}),
            lambda event: seen.append(getattr(event, "text", "")),
        )

    await run("ping")
    assert "".join(seen).strip() == "ping counter=1"
    await run("ping")
    assert "".join(seen).strip() == "ping counter=2"
    assert (tmp_path / "counter").read_text(encoding="utf-8").strip() == "2"


# ---- 端点 -----------------------------------------------------------------


@pytest.fixture
async def server(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    control = ControlServer(
        EchoHarness(), host="127.0.0.1", port=0, workspace_dir=workspace
    )
    await control.start()
    try:
        yield control
    finally:
        await control.close()


async def test_get_workspace_returns_gzip_of_directory(server, tmp_path: Path) -> None:
    (server.workspace_dir / "counter").write_text("7\n", encoding="utf-8")

    status, body = await request_loopback(
        server.port, "GET", WORKSPACE_ENDPOINT, headers=HEADERS, timeout_seconds=10
    )

    assert status == 200
    restored = tmp_path / "restored"
    unpack_into(body, restored)
    assert (restored / "counter").read_text(encoding="utf-8") == "7\n"


async def test_put_workspace_unpacks_into_directory(server, tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "counter").write_text("9\n", encoding="utf-8")

    status, _ = await request_loopback(
        server.port,
        "PUT",
        WORKSPACE_ENDPOINT,
        body=pack_directory(source),
        headers={**HEADERS, "Content-Type": WORKSPACE_CONTENT_TYPE},
        timeout_seconds=10,
    )

    assert status == 200
    assert (server.workspace_dir / "counter").read_text(encoding="utf-8") == "9\n"


async def test_put_workspace_rejects_escaping_archive(server) -> None:
    status, body = await request_loopback(
        server.port,
        "PUT",
        WORKSPACE_ENDPOINT,
        body=_archive({"../escape.txt": b"pwned"}),
        headers={**HEADERS, "Content-Type": WORKSPACE_CONTENT_TYPE},
        timeout_seconds=10,
    )

    assert status == 400
    assert b"unsafe_archive" in body
    assert not (server.workspace_dir.parent / "escape.txt").exists()


async def test_put_workspace_rejects_corrupt_archive(server) -> None:
    status, body = await request_loopback(
        server.port,
        "PUT",
        WORKSPACE_ENDPOINT,
        body=b"not an archive",
        headers={**HEADERS, "Content-Type": WORKSPACE_CONTENT_TYPE},
        timeout_seconds=10,
    )

    assert status == 500
    assert b"workspace_unpack_failed" in body


async def test_workspace_endpoint_rejects_other_methods(server) -> None:
    status, body = await request_loopback(
        server.port,
        "POST",
        WORKSPACE_ENDPOINT,
        body=b"",
        headers=HEADERS,
        timeout_seconds=10,
    )

    assert status == 405
    assert b"method_not_allowed" in body
