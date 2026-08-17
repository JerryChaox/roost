"""runtime_fingerprint 的判据语义（附录 I）。

防的回归：fingerprint 是"沙箱里的 runtime 过期了没有"的唯一判据，它错一个方向
就坏一整条 M6 路径——

- 漏报（改了源码却算出同一个 hash）：沙箱永远不更新，forced update 形同虚设；
- 误报（无关差异改变 hash）：每个 session 每次重启宿主都白挨一次替换式更新。

因此这里逐条钉：内容变则变、路径变则变、**dict 顺序变则不变**（文件表是映射，
不是序列；`DriverInstaller` 的收集顺序、宿主的合并顺序都不该改变结论）。
"""

from __future__ import annotations

from roost import DriverInstaller, runtime_fingerprint


class FakeInstaller:
    """只提供 fingerprint 依赖的那一面：沙箱内路径 → 内容。"""

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files

    @property
    def files(self) -> dict[str, bytes]:
        return dict(self._files)


BASE = {"/opt/roost/src/roost/a.py": b"alpha", "/opt/roost/src/roost/b.py": b"beta"}


def test_same_file_table_same_fingerprint() -> None:
    assert runtime_fingerprint(FakeInstaller(dict(BASE))) == runtime_fingerprint(
        FakeInstaller(dict(BASE))
    )


def test_insertion_order_does_not_matter() -> None:
    reversed_order = {path: BASE[path] for path in reversed(list(BASE))}
    assert list(reversed_order) != list(BASE)
    assert runtime_fingerprint(FakeInstaller(reversed_order)) == runtime_fingerprint(
        FakeInstaller(BASE)
    )


def test_content_change_changes_fingerprint() -> None:
    changed = dict(BASE) | {"/opt/roost/src/roost/b.py": b"beta2"}
    assert runtime_fingerprint(FakeInstaller(changed)) != runtime_fingerprint(
        FakeInstaller(BASE)
    )


def test_path_change_changes_fingerprint() -> None:
    """同样的字节搬到另一个路径是真实的运行时差异（模块搬家）。"""
    moved = {"/opt/roost/src/roost/a.py": b"alpha", "/opt/roost/src/roost/c.py": b"beta"}
    assert runtime_fingerprint(FakeInstaller(moved)) != runtime_fingerprint(
        FakeInstaller(BASE)
    )


def test_added_file_changes_fingerprint() -> None:
    extended = dict(BASE) | {"/opt/roost/src/roost/c.py": b""}
    assert runtime_fingerprint(FakeInstaller(extended)) != runtime_fingerprint(
        FakeInstaller(BASE)
    )


def test_path_content_boundary_is_unambiguous() -> None:
    """路径与内容之间必须有明确边界，否则拼接会撞车。"""
    left = FakeInstaller({"/ab": b"c"})
    right = FakeInstaller({"/a": b"bc"})
    assert runtime_fingerprint(left) != runtime_fingerprint(right)


def test_real_installer_is_stable_and_hex() -> None:
    installer = DriverInstaller()
    value = runtime_fingerprint(installer)
    assert len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    assert value == runtime_fingerprint(DriverInstaller())
