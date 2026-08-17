"""StateStore 实现子包。

职责分三层，互不混居（ROADMAP.md 反腐化拆分）：
- schema.py / pg_schema.py：表结构 DDL 与 status 词汇（各自方言一份）
- codec.py：行 <-> 核心类型的纯函数编解码（两个实现共用）
- sqlite.py / postgres.py：SQL 语句与 IO 编排

`PostgresStateStore` 是**惰性导出**（PEP 562 的模块级 `__getattr__`）：它依赖可选
extra `roost[postgres]`（asyncpg），而核心必须零运行时依赖——没装 extra 的环境
`import roost` 照常成功，只有真去取这个名字时才会 import 到驱动。
"""

from typing import TYPE_CHECKING, Any

from .sqlite import SQLiteStateStore

if TYPE_CHECKING:  # 类型检查器要看得见真实符号，运行期仍然惰性
    from .postgres import PostgresStateStore

__all__ = ["SQLiteStateStore", "PostgresStateStore"]


def __getattr__(name: str) -> Any:
    if name == "PostgresStateStore":
        from .postgres import PostgresStateStore

        return PostgresStateStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
