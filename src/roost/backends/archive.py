"""upload 用的内存 tar 构造。

纯函数、无 IO：给定 `{容器内绝对路径: bytes}` 产出可喂给 `docker cp - <id>:/`
的 tar 字节。与 docker CLI 调用分开，因此可独立测试且不需要 daemon。
"""

from __future__ import annotations

import io
import posixpath
import tarfile

__all__ = ["build_tar"]

_FILE_MODE = 0o644
_DIR_MODE = 0o755


def _normalize(path: str) -> str:
    """容器内绝对路径 → tar 成员名（相对根，无 . / .. 分量）。"""
    if not path.startswith("/"):
        raise ValueError(f"upload path must be absolute: {path!r}")
    name = posixpath.normpath(path).lstrip("/")
    if not name:
        raise ValueError(f"upload path must name a file, not the root: {path!r}")
    if any(part == ".." for part in name.split("/")):
        raise ValueError(f"upload path must not escape the root: {path!r}")
    return name


def _parent_dirs(name: str) -> list[str]:
    parts = name.split("/")[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def build_tar(files: dict[str, bytes]) -> bytes:
    """打包 files 为 tar 字节流；父目录补齐为目录成员，成员顺序确定。"""
    members = {_normalize(path): data for path, data in files.items()}
    directories: set[str] = set()
    for name in members:
        directories.update(_parent_dirs(name))

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(directories):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = _DIR_MODE
            info.mtime = 0
            info.uname = "root"
            info.gname = "root"
            tar.addfile(info)
        for name in sorted(members):
            data = members[name]
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = _FILE_MODE
            info.mtime = 0
            info.uname = "root"
            info.gname = "root"
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()
