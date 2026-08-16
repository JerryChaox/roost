"""StateStore 实现子包。

职责分三层，互不混居（ROADMAP.md 反腐化拆分）：
- schema.py：表结构 DDL 与 status 词汇
- codec.py：行 <-> 核心类型的纯函数编解码
- sqlite.py：SQL 语句与 IO 编排
"""

from .sqlite import SQLiteStateStore

__all__ = ["SQLiteStateStore"]
