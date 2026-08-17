"""工作区目录的 tar.gz 打包 / 解包（沙箱侧）。

契约见 CONTRACTS.md《附录 G：M4 持久化整合契约》。本模块只承担一类职责：**把
一个目录变成字节、把字节变回一个目录**，并保证解包不会写到目录外面。它不认识
HTTP、不认识 session、不认识 SnapshotStore——路由在 server.py，"什么时候备份"
在 host 侧的 backup.py。

三条实现约束：

1. **成员集合是"目录 + 普通文件"这一对，打包与解包必须对称**。打包跳过符号链接
   与设备/FIFO 一类特殊文件，解包也拒绝它们（400）。理由不是洁癖：符号链接的
   逃逸不在路径里而在 link target 上，容许它就要额外维护一套 target 校验，而
   workspace 的语义（agent 的工作目录内容跨沙箱搬运）并不需要它。两侧同一条
   规则，就不会出现"自己打的包自己拒收"。
2. **路径校验只信自己的判定，不信 tarfile 的默认**：成员名必须是相对路径、
   规范化后不得以 `..` 起头、不得是绝对路径或含盘符。违规抛
   `UnsafeArchiveError`，由 server.py 翻译成 400。刻意不用 `extractall(filter=…)`：
   那个参数在 3.11 的补丁版本之间行为不一，而这里要的判定只有三行。
3. **解包是覆盖而不是清空重建**：同名文件覆写，未在归档里出现的既有文件保留。
   恢复发生在 cold boot 后的空工作区上（附录 G 的接线点），因此两种语义在契约
   路径上等价；选覆盖是因为它不会在"归档不完整"时把工作区变得更糟（I2 的
   never worse off 精神）。

零运行时依赖：只用标准库。
"""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path, PurePosixPath

from ..protocol import ENV_PREFIX

__all__ = [
    "UnsafeArchiveError",
    "DEFAULT_WORKSPACE_DIR",
    "ENV_WORKSPACE_DIR",
    "workspace_dir_from_env",
    "ensure_workspace_dir",
    "pack_directory",
    "unpack_into",
]

# 默认落在**当前用户的家目录**下，而不是 `/`：沙箱不一定以 root 运行（E2B 的默认
# 用户是 `user`），`/workspace` 在非 root 沙箱里建不出来（PermissionError 13），
# 工作区备份/恢复会整条失败。`~` 在 driver 启动时 expanduser 解析——root 下是
# /root/workspace，E2B 下是 /home/user/workspace。
DEFAULT_WORKSPACE_DIR = "~/workspace"
ENV_WORKSPACE_DIR = f"{ENV_PREFIX}WORKSPACE_DIR"


class UnsafeArchiveError(ValueError):
    """归档里有逃逸目录或不受支持的成员；调用方应答 400。"""


def workspace_dir_from_env(env: dict[str, str] | None = None) -> Path:
    """工作区目录：`ROOST_WORKSPACE_DIR`，缺省 `~/workspace`。

    `~` 在这里解析（`expanduser`），显式覆盖值同样享受这个解析——宿主因此可以写
    `ROOST_WORKSPACE_DIR=~/agent`，而不必知道沙箱以哪个用户跑。
    """
    source = os.environ if env is None else env
    raw = source.get(ENV_WORKSPACE_DIR) or DEFAULT_WORKSPACE_DIR
    return Path(raw).expanduser()


def ensure_workspace_dir(path: Path) -> None:
    """确保工作区目录存在。目录已存在或成功创建后返回，其余情况抛 OSError。"""
    path.mkdir(parents=True, exist_ok=True)


# ---- 打包 -----------------------------------------------------------------


def pack_directory(root: Path) -> bytes:
    """把 `root` 的内容打成 tar.gz 字节。

    归档里的成员名是相对 `root` 的 posix 路径（无 `./` 前缀）。目录不存在或为空
    时返回一个合法的空归档——"没有工作区"和"工作区是空的"对恢复方等价。
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=6) as archive:
        if root.is_dir():
            for path, name in _walk(root):
                info = archive.gettarinfo(str(path), arcname=name)
                if info.isdir():
                    archive.addfile(info)
                elif info.isfile():
                    with path.open("rb") as handle:
                        archive.addfile(info, handle)
                # 其余类型（符号链接/设备/FIFO）按上面第 1 条跳过。
    return buffer.getvalue()


def _walk(root: Path) -> list[tuple[Path, str]]:
    """深度优先收集 `root` 下的目录与普通文件，按名字排序（归档可复现）。"""
    entries: list[tuple[Path, str]] = []
    stack = [(root, "")]
    while stack:
        current, prefix = stack.pop()
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for child in children:
            name = f"{prefix}{child.name}"
            if child.is_symlink():
                continue
            if child.is_dir():
                entries.append((child, name))
                stack.append((child, f"{name}/"))
            elif child.is_file():
                entries.append((child, name))
    entries.sort(key=lambda item: item[1])
    return entries


# ---- 解包 -----------------------------------------------------------------


def unpack_into(data: bytes, root: Path) -> None:
    """把 tar.gz 字节解包覆盖进 `root`。

    非法成员（绝对路径 / 逃逸 / 非普通文件目录）抛 `UnsafeArchiveError`；归档本身
    损坏抛 `tarfile.TarError`（调用方翻译成 500）。校验在写盘之前全部做完：
    一个非法成员就整包拒绝，不留下"解了一半"的工作区。
    """
    root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            if not (member.isfile() or member.isdir()):
                raise UnsafeArchiveError(
                    f"归档成员 {member.name!r} 类型不受支持（仅接受目录与普通文件）"
                )
            _safe_relative(member.name)
        for member in members:
            target = root / _safe_relative(member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:                      # 理论不可达（isfile 已判过）
                raise UnsafeArchiveError(f"归档成员 {member.name!r} 无内容")
            with source, target.open("wb") as handle:
                while chunk := source.read(1 << 20):
                    handle.write(chunk)


def _safe_relative(name: str) -> PurePosixPath:
    """成员名 → 目录内的相对路径；越界一律抛 UnsafeArchiveError。"""
    if not name or name in (".", "./"):
        raise UnsafeArchiveError("归档成员名为空")
    if "\\" in name:
        raise UnsafeArchiveError(f"归档成员 {name!r} 含反斜杠")
    candidate = PurePosixPath(name)
    if candidate.is_absolute():
        raise UnsafeArchiveError(f"归档成员 {name!r} 是绝对路径")
    parts = [part for part in candidate.parts if part != "."]
    if any(part == ".." for part in parts):
        raise UnsafeArchiveError(f"归档成员 {name!r} 逃逸出工作区目录")
    if not parts:
        raise UnsafeArchiveError(f"归档成员 {name!r} 不指向任何路径")
    return PurePosixPath(*parts)
