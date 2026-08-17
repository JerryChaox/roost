"""活性探测 —— 从 /proc 判断"沙箱里还有没有真的在干活"。

契约见 CONTRACTS.md《附录 M — 决策矩阵》：liveness clock 竭而 progress clock 未竭
时，宿主经 `SandboxBackend.exec` 跑一次本脚本；输出 ACTIVE 才 silence-defer，
否则 kill/restart。脚本随 driver 一起分发（它就住在 roost 包里，DriverInstaller
整包搬运，因此**天然计入 runtime fingerprint**——探测逻辑变了，沙箱里的运行时
就该被判定为过期）。

为什么需要它：liveness 与 progress 两个时钟都只能看见"driver 报告了什么"。
一个正在等 90 秒 API 响应、或正在跑一个长 tool 的 agent，两个时钟都可能同时安静，
而它其实好得很。/proc 是**内核**对"这些进程在干什么"的记录——判定以它为准，
时钟只是佐证。

判定规则（刻意写死、可单测；`decide_active` 是纯函数）：

- **driver 进程本身**只有 state R（running）/ D（不可中断 IO）才算 ACTIVE。
  它的 asyncio 事件循环常年睡在 epoll 上，那是 idle 的定义，不是活着的证据。
- **子孙进程**（harness 起的 CLI、tool、子解释器）state R/D 算 ACTIVE；state S
  时除非它睡在一个明确表示"自己在等自己"的地方（futex / nanosleep / pause /
  sigtimedwait），否则也算 ACTIVE——一个活着的子进程阻塞在 IO 上，正是"慢而活"
  的样子。
- 找不到 driver 进程 → 非 ACTIVE（这种沙箱本来就该被 restart）。

两侧误判的代价刻意不对称：误判 ACTIVE 只是多等一会儿（progress clock 仍然会
在 180s 后独立把真 hang 判死，见矩阵第二行"即使 ACTIVE 也杀"），误判非 ACTIVE
则会杀掉一个正在正常干活的沙箱。因此子孙进程的默认是 ACTIVE。

输出：单行 JSON 到 stdout，退出码恒为 0——探测失败也是一种观测结果，不该让
宿主侧的 exec 变成异常路径。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

__all__ = [
    "ProcessInfo",
    "ProbeResult",
    "DRIVER_ARG",
    "IDLE_WCHAN_PREFIXES",
    "decide_active",
    "scan",
    "probe",
    "parse_probe_output",
    "main",
]

#: driver 进程的识别特征：命令行里有一个**恰好等于**它的参数。
#: 探测脚本自己是 `python -m roost.driver.probe`，停止脚本是 `roost.driver.stop`，
#: 都不会等于这个串——识别规则因此不需要再排除自己。
DRIVER_ARG = "roost.driver"

#: 子孙进程睡在这些地方**不**算 ACTIVE：它们表示"在等自己"（锁、定时器、信号），
#: 而不是"在等外面的世界"。前缀匹配，覆盖各内核版本的符号变体。
IDLE_WCHAN_PREFIXES = (
    "futex",
    "do_nanosleep",
    "hrtimer_nanosleep",
    "do_sigtimedwait",
    "pause",
    "do_signal_stop",
)

_RUNNING_STATES = frozenset({"R", "D"})
_DEAD_STATES = frozenset({"Z", "X", "x"})


@dataclass(frozen=True)
class ProcessInfo:
    """一个进程在 /proc 里的四个事实。字段名同时是 JSON 里的键。"""

    pid: int
    ppid: int
    comm: str
    state: str
    wchan: str
    syscall: str


@dataclass(frozen=True)
class ProbeResult:
    """探测输出（宿主侧解析成这个形状）。

    `raw` 保留原始 JSON：诊断快照要原样进 ops details（附录 M 的观测纪律——
    沙箱被终止后就再也取不到证据了）。
    """

    active: bool
    reason: str
    driver_pid: int | None
    processes: list[dict]
    raw: str = ""


def decide_active(
    processes: Sequence[ProcessInfo], *, driver_pid: int | None
) -> tuple[bool, str]:
    """纯函数判定：(是否 ACTIVE, 一句话理由)。规则见模块 docstring。"""
    if driver_pid is None:
        return False, "driver_not_found"
    for info in processes:
        if info.state in _DEAD_STATES:
            continue
        if info.state in _RUNNING_STATES:
            return True, f"pid {info.pid} ({info.comm}) state={info.state}"
        if info.syscall == "running":
            return True, f"pid {info.pid} ({info.comm}) syscall=running"
        if info.pid == driver_pid:
            # driver 自己睡着 = 事件循环在 epoll 上待命，这是 idle 的定义。
            continue
        if not _is_idle_wchan(info.wchan):
            return True, f"pid {info.pid} ({info.comm}) wchan={info.wchan or '?'}"
    return False, "no_active_process"


def _is_idle_wchan(wchan: str) -> bool:
    name = wchan.strip()
    if not name or name == "0":
        # wchan 读不到（权限/内核配置）时不做"它在偷懒"的推断：子孙进程存在本身
        # 就是活的证据，按 ACTIVE 处理（见 docstring 的不对称说明）。
        return False
    return name.startswith(IDLE_WCHAN_PREFIXES)


# -- /proc 扫描 -----------------------------------------------------------


def scan(proc_root: str | os.PathLike[str] = "/proc") -> tuple[int | None, list[ProcessInfo]]:
    """扫出 driver 进程及其全部子孙。

    进程可能在扫描过程中消失——每一次读都容忍 OSError 并跳过，绝不因为一个
    竞态让整次探测失败。
    """
    root = Path(proc_root)
    all_procs: dict[int, ProcessInfo] = {}
    driver_pid: int | None = None
    for entry in _pid_dirs(root):
        pid = int(entry.name)
        info = _read_process(entry, pid)
        if info is None:
            continue
        all_procs[pid] = info
        if driver_pid is None and _is_driver(entry):
            driver_pid = pid
    if driver_pid is None:
        return None, []
    return driver_pid, _tree(driver_pid, all_procs)


def _pid_dirs(root: Path) -> Iterable[Path]:
    try:
        entries = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return []
    return [entry for entry in entries if entry.name.isdigit()]


def _is_driver(entry: Path) -> bool:
    try:
        raw = (entry / "cmdline").read_bytes()
    except OSError:
        return False
    return DRIVER_ARG in raw.decode("utf-8", "replace").split("\0")


def _read_process(entry: Path, pid: int) -> ProcessInfo | None:
    try:
        stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # comm 可以包含空格和右括号，所以按**最后一个** ')' 切开，前面是 pid，
    # 后面第一个字段是 state、第二个是 ppid（proc(5) 的字段序）。
    close = stat.rfind(")")
    open_paren = stat.find("(")
    if close == -1 or open_paren == -1:
        return None
    comm = stat[open_paren + 1 : close]
    rest = stat[close + 2 :].split()
    if len(rest) < 2:
        return None
    try:
        ppid = int(rest[1])
    except ValueError:
        return None
    return ProcessInfo(
        pid=pid,
        ppid=ppid,
        comm=comm,
        state=rest[0],
        wchan=_read_text(entry / "wchan"),
        syscall=_read_syscall(entry / "syscall"),
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_syscall(path: Path) -> str:
    """/proc/<pid>/syscall 的第一个 token：系统调用号，或字面量 "running"。"""
    raw = _read_text(path)
    if not raw:
        return ""
    return raw.split()[0]


def _tree(root_pid: int, procs: dict[int, ProcessInfo]) -> list[ProcessInfo]:
    children: dict[int, list[int]] = {}
    for info in procs.values():
        children.setdefault(info.ppid, []).append(info.pid)
    ordered: list[ProcessInfo] = []
    stack = [root_pid]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen or pid not in procs:
            continue
        seen.add(pid)
        ordered.append(procs[pid])
        stack.extend(sorted(children.get(pid, ()), reverse=True))
    return ordered


# -- 入口 -----------------------------------------------------------------


def probe(proc_root: str | os.PathLike[str] = "/proc") -> dict:
    driver_pid, processes = scan(proc_root)
    active, reason = decide_active(processes, driver_pid=driver_pid)
    return {
        "active": active,
        "reason": reason,
        "driver_pid": driver_pid,
        "processes": [asdict(info) for info in processes],
    }


def parse_probe_output(stdout: str) -> ProbeResult:
    """宿主侧解析。**任何**解析失败都当成"没有活性证据"，绝不抛。

    探测是决策的输入之一，不是它自己的失败路径：探不出来时把判定交回给两个
    时钟（矩阵第四行 → kill/restart），比让 runner 在收拾卡死沙箱的路上再炸一次好。
    """
    text = stdout.strip()
    try:
        obj = json.loads(text.splitlines()[-1]) if text else None
    except (ValueError, IndexError):
        obj = None
    if not isinstance(obj, dict):
        return ProbeResult(
            active=False, reason="probe_unparseable", driver_pid=None,
            processes=[], raw=text[:2000],
        )
    processes = obj.get("processes")
    return ProbeResult(
        active=bool(obj.get("active", False)),
        reason=str(obj.get("reason", "")),
        driver_pid=obj.get("driver_pid") if isinstance(obj.get("driver_pid"), int) else None,
        processes=processes if isinstance(processes, list) else [],
        raw=text[:2000],
    )


def main(argv: list[str] | None = None) -> int:
    del argv                                    # 无参数面：探测就是探测
    print(json.dumps(probe(), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
