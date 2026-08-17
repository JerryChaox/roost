"""运行时 fingerprint —— "沙箱里的 driver 是不是过期了"的唯一判据。

契约见 CONTRACTS.md《附录 I：M6 零停机 forced update 契约》。

设计要点：

- **对文件表本身取哈希，而不是对版本号/时间戳**：DriverInstaller 的产出（沙箱内
  路径 → 字节）就是"沙箱里将会跑什么"的完整描述。任何一处源码改动都会改变它，
  而无关的改动（宿主进程重启、包版本号未 bump、上传顺序）不会——判据与被判据的
  事物之间没有需要人工维护的中间层，因此不会腐化。
- **顺序无关、路径参与哈希**：文件表是映射不是序列，dict 的迭代顺序不该改变结论；
  但"同样的内容换个路径"是真实的运行时差异（模块搬家），必须改变结论。故按路径
  排序后把 (路径, 内容) 逐条喂进 sha256，并写入长度前缀——没有长度前缀时，
  `{"ab": b"c"}` 与 `{"a": b"bc"}` 这类拼接会撞成同一个摘要。
"""

from __future__ import annotations

import hashlib
from typing import Protocol

__all__ = ["runtime_fingerprint", "RuntimeFiles"]


class RuntimeFiles(Protocol):
    """fingerprint 只依赖 DriverInstaller 的这一面：沙箱内路径 → 内容。"""

    @property
    def files(self) -> dict[str, bytes]: ...


def runtime_fingerprint(installer: RuntimeFiles) -> str:
    """对 DriverInstaller 的文件表计算 sha256（路径排序 + 内容）。"""
    digest = hashlib.sha256()
    files = installer.files
    for path in sorted(files):
        content = files[path]
        encoded = path.encode("utf-8")
        digest.update(f"{len(encoded)}:".encode("ascii"))
        digest.update(encoded)
        digest.update(f"{len(content)}:".encode("ascii"))
        digest.update(content)
    return digest.hexdigest()
