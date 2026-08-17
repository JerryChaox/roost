"""FileSnapshotStore —— SnapshotStore port 的本地文件系统实现。

契约见 CONTRACTS.md《宿主 ports》SnapshotStore 与《附录 C：M4 SnapshotStore
实现契约》。本模块只承担一类职责：把 opaque key 映射到 root 下的一个文件，并保证
写入原子。key 的语义（怎么从 session_id 派生）归宿主的 SnapshotKeyFn，本模块
**不解释 key 结构**——它只是一串字节标识。

两条实现约束：

1. **key → 文件名用 URL 百分号编码（safe=''）**。因此 '/'、'..'、空格、unicode
   全部被编码掉，落盘永远是 root 下的单层平坦文件，不存在路径穿越，也不需要
   对 key 做任何合法性猜测。
2. **写入原子**：先写同目录临时文件（同目录才能保证 rename 不跨设备）、fsync、
   再 `os.replace` 覆盖目标。读者要么看到旧内容要么看到新内容，永远看不到半截
   文件——这正是 DESIGN.md I2 要的：snapshot 写失败不能污染上一份可恢复状态。

零运行时依赖：只用标准库。async 接口用 `asyncio.to_thread` 包装同步 IO——文件
IO 没有线程亲和性（不同于 sqlite3 连接），共享线程池是合适的。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from urllib.parse import quote

__all__ = ["FileSnapshotStore"]

_TMP_PREFIX = ".roost-snapshot-"
_TMP_SUFFIX = ".part"


class FileSnapshotStore:
    """把 snapshot 落在 `root` 目录下的 SnapshotStore 实现。

    目录按需创建（put 时）。适合单机 / 单进程宿主与测试；跨主机恢复请用
    `S3SnapshotStore`。
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, key: str) -> Path:
        """key → 落盘路径。公开出来是为了让宿主能做备份/清理，不参与热路径。"""
        if not key:
            raise ValueError("snapshot key must not be empty")
        return self._root / quote(key, safe="")

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._put_sync, key, data)

    async def get(self, key: str) -> bytes | None:
        return await asyncio.to_thread(self._get_sync, key)

    # ---- 同步实现（在线程里跑） ----

    def _put_sync(self, key: str, data: bytes) -> None:
        target = self.path_for(key)
        self._root.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=self._root, prefix=_TMP_PREFIX, suffix=_TMP_SUFFIX
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, target)
        except BaseException:
            # 失败路径必须不留垃圾，也不能动已有的 target。
            tmp_path.unlink(missing_ok=True)
            raise
        self._fsync_dir()

    def _fsync_dir(self) -> None:
        """让 rename 本身落盘。尽力而为：不支持目录 fsync 的平台直接跳过。"""
        try:
            fd = os.open(self._root, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _get_sync(self, key: str) -> bytes | None:
        try:
            return self.path_for(key).read_bytes()
        except FileNotFoundError:
            return None
