"""upload tar 构造的单测（纯函数，不需要 docker）。

防回归目标：路径归一化与父目录补齐——它们决定 `docker cp -` 能否落盘到
容器内的绝对路径，且是 upload 唯一的非平凡逻辑。
"""

from __future__ import annotations

import io
import tarfile

import pytest

from roost.backends import build_tar


def _members(tar_bytes: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r") as tar:
        return {member.name: member for member in tar.getmembers()}


def test_absolute_paths_become_root_relative_members_with_parent_dirs():
    tar_bytes = build_tar({"/srv/roost/a.txt": b"body"})

    members = _members(tar_bytes)

    assert set(members) == {"srv", "srv/roost", "srv/roost/a.txt"}
    assert members["srv"].isdir()
    assert members["srv/roost"].isdir()
    assert members["srv/roost/a.txt"].size == 4


def test_content_round_trips_byte_exact():
    payload = bytes(range(256))

    with tarfile.open(fileobj=io.BytesIO(build_tar({"/data/blob.bin": payload})), mode="r") as tar:
        extracted = tar.extractfile("data/blob.bin")
        assert extracted is not None
        assert extracted.read() == payload


@pytest.mark.parametrize("path", ["relative.txt", "./relative.txt", "/", "/.."])
def test_rejects_non_absolute_or_rootless_paths(path: str):
    with pytest.raises(ValueError):
        build_tar({path: b""})


def test_empty_mapping_produces_empty_archive():
    assert _members(build_tar({})) == {}
