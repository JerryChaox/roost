"""活性探测（CONTRACTS.md 附录 M 的 activity probe）：/proc 说了什么、怎么判。

防的回归：判定矩阵第三/四行的分岔完全押在这个脚本的输出上。判错 ACTIVE 的代价
是多等一会儿（progress clock 仍会独立判死真 hang），判错"非 ACTIVE"的代价是
**杀掉一个正在干活的沙箱**——两侧刻意不对称，这里把这个不对称本身钉住。

扫描用假 /proc 目录树（不依赖本机是 Linux，macOS 上照样跑）；宿主侧的解析则钉
"探测失败也不得抛"——它是决策的输入，不是决策路径上的第二个失败源。
"""

from __future__ import annotations

import json
from pathlib import Path

from roost.driver.probe import (
    DRIVER_ARG,
    ProcessInfo,
    decide_active,
    parse_probe_output,
    probe,
    scan,
)


def write_process(
    root: Path,
    pid: int,
    *,
    ppid: int = 1,
    comm: str = "python3",
    state: str = "S",
    cmdline: tuple[str, ...] = ("python3",),
    wchan: str = "ep_poll",
    syscall: str = "232 0x1",
) -> None:
    entry = root / str(pid)
    entry.mkdir()
    (entry / "stat").write_text(f"{pid} ({comm}) {state} {ppid} 0 0 0 -1 0\n")
    (entry / "cmdline").write_bytes(("\0".join(cmdline) + "\0").encode())
    (entry / "wchan").write_text(wchan)
    (entry / "syscall").write_text(syscall)


def driver_cmdline() -> tuple[str, ...]:
    return ("python3", "-m", DRIVER_ARG)


def info(**overrides) -> ProcessInfo:
    base = dict(pid=7, ppid=1, comm="python3", state="S", wchan="ep_poll", syscall="232")
    base.update(overrides)
    return ProcessInfo(**base)


# ---- 判定规则 -------------------------------------------------------------


def test_sleeping_driver_alone_is_not_active() -> None:
    """driver 独自睡在 epoll 上 = idle 的定义，不是活着的证据。"""
    active, reason = decide_active([info(pid=7)], driver_pid=7)
    assert active is False
    assert reason == "no_active_process"


def test_running_driver_is_active() -> None:
    active, _ = decide_active([info(pid=7, state="R")], driver_pid=7)
    assert active is True


def test_driver_in_uninterruptible_io_is_active() -> None:
    """state D：内核说它正卡在 IO 上——这是活着，不是死了。"""
    active, _ = decide_active([info(pid=7, state="D")], driver_pid=7)
    assert active is True


def test_child_blocked_on_io_is_active() -> None:
    """子进程阻塞在外部 IO 上（等 API 响应）正是"慢而活"的样子。"""
    active, reason = decide_active(
        [info(pid=7), info(pid=11, ppid=7, comm="node", wchan="sk_wait_data")],
        driver_pid=7,
    )
    assert active is True
    assert "11" in reason


def test_child_sleeping_on_a_futex_is_not_active() -> None:
    """子进程在等自己（锁/定时器/信号）不算活性证据。"""
    active, _ = decide_active(
        [
            info(pid=7),
            info(pid=11, ppid=7, comm="node", wchan="futex_wait_queue_me"),
        ],
        driver_pid=7,
    )
    assert active is False


def test_zombie_children_do_not_count() -> None:
    """僵尸进程什么都不在做，尽管它还挂在进程表里。"""
    active, _ = decide_active(
        [info(pid=7), info(pid=11, ppid=7, state="Z", wchan="")], driver_pid=7
    )
    assert active is False


def test_unreadable_wchan_defaults_to_active_for_children() -> None:
    """wchan 读不到时不推断"它在偷懒"：活着的子进程本身就是证据（不对称的那一侧）。"""
    active, _ = decide_active(
        [info(pid=7), info(pid=11, ppid=7, wchan="")], driver_pid=7
    )
    assert active is True


def test_missing_driver_is_not_active() -> None:
    """driver 进程都没了：这种沙箱本来就该被 restart。"""
    active, reason = decide_active([], driver_pid=None)
    assert (active, reason) == (False, "driver_not_found")


# ---- /proc 扫描 -----------------------------------------------------------


def test_scan_finds_the_driver_and_only_its_descendants(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    root.mkdir()
    write_process(root, 1, ppid=0, comm="sh", cmdline=("sh",))
    write_process(root, 7, ppid=1, cmdline=driver_cmdline())
    write_process(root, 11, ppid=7, comm="node", cmdline=("node", "cli.js"))
    write_process(root, 12, ppid=11, comm="rg", cmdline=("rg", "needle"))
    write_process(root, 99, ppid=1, comm="unrelated", cmdline=("sleep", "1"))

    driver_pid, processes = scan(root)

    assert driver_pid == 7
    assert sorted(p.pid for p in processes) == [7, 11, 12]   # 孙进程算，邻居不算


def test_scan_ignores_the_probe_and_stop_helpers(tmp_path: Path) -> None:
    """识别规则是"有一个参数**恰好等于** roost.driver"——探测/停止脚本不会自指。"""
    root = tmp_path / "proc"
    root.mkdir()
    write_process(root, 5, cmdline=("python3", "-m", "roost.driver.probe"))
    write_process(root, 6, cmdline=("python3", "-m", "roost.driver.stop"))

    assert scan(root) == (None, [])


def test_probe_output_is_one_json_line(tmp_path: Path) -> None:
    root = tmp_path / "proc"
    root.mkdir()
    write_process(root, 7, cmdline=driver_cmdline(), state="R")

    payload = probe(root)

    assert payload["active"] is True
    assert payload["driver_pid"] == 7
    assert json.loads(json.dumps(payload))["processes"][0]["pid"] == 7


# ---- 宿主侧解析 -----------------------------------------------------------


def test_parse_reads_a_well_formed_line() -> None:
    result = parse_probe_output(
        json.dumps({"active": True, "reason": "pid 9 state=R", "driver_pid": 9,
                    "processes": [{"pid": 9}]})
    )
    assert (result.active, result.driver_pid, result.reason) == (True, 9, "pid 9 state=R")
    assert result.processes == [{"pid": 9}]


def test_parse_survives_leading_noise() -> None:
    """沙箱里任何东西都可能往 stdout 里插一行（shell profile、告警）。取最后一行。"""
    result = parse_probe_output(
        "warning: something\n" + json.dumps({"active": True, "reason": "ok"})
    )
    assert result.active is True


def test_parse_never_raises_on_garbage() -> None:
    """探不出来 = 没有活性证据，交回给两个时钟；绝不在收拾卡死沙箱的路上再炸一次。"""
    for junk in ("", "not json", "[1,2,3]", "null"):
        result = parse_probe_output(junk)
        assert result.active is False
        assert result.reason == "probe_unparseable"
