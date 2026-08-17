"""snapshot —— SnapshotStore port 的两个实现（M4 · Durability）。

- `FileSnapshotStore`：本地文件系统，原子写，单机 / 测试用。
- `S3SnapshotStore`：S3 兼容对象存储（endpoint_url 可覆盖 → MinIO / GCS HMAC）。
- `sigv4`：AWS Signature V4 纯函数，供 S3 实现使用，独立可测。

两者都只承诺 `put` / `get`（CONTRACTS.md《宿主 ports》）。key 由宿主的
SnapshotKeyFn 派生，对本包 opaque。
"""

from __future__ import annotations

from . import sigv4
from .fs import FileSnapshotStore
from .s3 import S3Error, S3SnapshotStore

__all__ = [
    "FileSnapshotStore",
    "S3Error",
    "S3SnapshotStore",
    "sigv4",
]
