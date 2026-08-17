"""沙箱侧 driver 子系统（仅标准库；打包进沙箱 artifact）。

契约见 CONTRACTS.md《附录 B》与仓库根 PROTOCOL.md。模块分工（一个模块一类变更原因）：

- `httpd.py`    —— HTTP 报文的读写（不认识 roost 概念）
- `server.py`   —— 端点路由与 wire 编解码边界
- `registry.py` —— turn 条目状态机（I1 driver 侧幂等）
- `worker.py`   —— 单 harness worker 的 FIFO 执行循环与终态兜底
- `emit.py`     —— seq 分配、per-turn 事件缓存、长轮询等待原语
- `harness.py`  —— Harness port 与 M2 的 EchoHarness
- `liveness.py` —— driver 侧的 liveness 时钟（附录 M 的 `liveness_quiet_ms`）
- `probe.py`    —— 活性探测脚本（`python -m roost.driver.probe`，扫 /proc）
- `stop.py`     —— 停掉 driver 进程（`python -m roost.driver.stop`，restart 阶梯用）
- `__main__.py` —— `python -m roost.driver` 进程入口

这些名字是 driver 内部实现，**不进 `roost` 顶层导出**：宿主只经
`roost.control` 的协议客户端与 driver 打交道。

一个例外与 `control/envelope.py` 同理：`probe.py` 里的 `ProbeResult` /
`parse_probe_output` 是**探测输出的 wire 形状**，产出端在沙箱、解析端在宿主
（sessions.py）。两端共用同一份定义，而不是各写一份 JSON 解析。
"""

from .harness import EchoHarness, Harness
from .registry import TurnEntry, TurnRegistry
from .server import ControlServer

__all__ = [
    "ControlServer",
    "Harness",
    "EchoHarness",
    "TurnRegistry",
    "TurnEntry",
]
