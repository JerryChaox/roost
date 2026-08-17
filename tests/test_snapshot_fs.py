"""FileSnapshotStore 行为测试（CONTRACTS.md 附录 C）。

保护三件事：roundtrip（含 key 不被解释）、写入原子性（失败不污染上一份 snapshot、
不留垃圾）、未命中返回 None（恢复路径靠它区分"没有快照"与"读失败"）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from roost.snapshot import FileSnapshotStore


@pytest.fixture
def store(tmp_path: Path) -> FileSnapshotStore:
    return FileSnapshotStore(tmp_path / "snapshots")


async def test_put_then_get_roundtrip(store: FileSnapshotStore) -> None:
    await store.put("session-1", b"\x00binary\xffpayload")
    assert await store.get("session-1") == b"\x00binary\xffpayload"


async def test_put_overwrites_previous_snapshot(store: FileSnapshotStore) -> None:
    await store.put("session-1", b"old")
    await store.put("session-1", b"new-and-longer")
    assert await store.get("session-1") == b"new-and-longer"


async def test_get_missing_key_returns_none(store: FileSnapshotStore) -> None:
    assert await store.get("never-written") is None


async def test_get_missing_key_returns_none_when_root_absent(
    store: FileSnapshotStore,
) -> None:
    # root 目录只在首次 put 时创建；此前 get 必须是未命中而不是异常。
    assert not store.root.exists()
    assert await store.get("session-1") is None


@pytest.mark.parametrize(
    "key",
    [
        "sess/with/slashes",
        "../escape",
        "with space and ?query#frag",
        "unicode-会话-ሴ",
        "a" * 120,
    ],
)
async def test_keys_are_opaque_and_stay_flat_under_root(
    store: FileSnapshotStore, key: str
) -> None:
    """key 不被解释：任何字节都编码进单层文件名，不产生子目录也不逃出 root。"""
    await store.put(key, b"payload")
    assert await store.get(key) == b"payload"

    path = store.path_for(key)
    assert path.parent == store.root
    assert path.resolve().parent == store.root.resolve()
    assert [p.name for p in store.root.iterdir()] == [path.name]


async def test_distinct_keys_do_not_collide(store: FileSnapshotStore) -> None:
    await store.put("a/b", b"first")
    await store.put("a%2Fb", b"second")
    assert await store.get("a/b") == b"first"
    assert await store.get("a%2Fb") == b"second"


async def test_empty_key_is_rejected(store: FileSnapshotStore) -> None:
    with pytest.raises(ValueError):
        await store.put("", b"payload")


async def test_write_is_atomic_via_same_dir_tmp_then_rename(
    store: FileSnapshotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """观察到的 rename 必须是同目录 tmp → 目标（跨设备 rename 不原子）。"""
    seen: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src, dst, **kwargs):  # type: ignore[no-untyped-def]
        seen.append((str(src), str(dst)))
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr("roost.snapshot.fs.os.replace", spy)
    await store.put("session-1", b"payload")

    assert len(seen) == 1
    src, dst = seen[0]
    assert Path(src).parent == store.root
    assert Path(src).name.startswith(".roost-snapshot-")
    assert dst == str(store.path_for("session-1"))


async def test_failed_write_keeps_previous_snapshot_and_leaves_no_temp_file(
    store: FileSnapshotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rename 阶段失败：旧内容必须完好，且不留半截临时文件。

    这是 I2 的具体要求——快照写失败不能把上一份可恢复状态毁掉。
    """
    await store.put("session-1", b"good-snapshot")

    def boom(src, dst, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("disk full")

    monkeypatch.setattr("roost.snapshot.fs.os.replace", boom)
    with pytest.raises(OSError):
        await store.put("session-1", b"half-written-garbage")

    assert await store.get("session-1") == b"good-snapshot"
    assert [p.name for p in store.root.iterdir()] == [
        store.path_for("session-1").name
    ]


async def test_reader_never_sees_partial_content(
    store: FileSnapshotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目标路径在 rename 之前不存在中间态：新数据已全部写入 tmp，此刻读到的仍是旧内容。"""
    await store.put("session-1", b"v1")

    real_replace = os.replace
    observed: list[bytes | None] = []

    def replace_after_peek(src, dst, **kwargs):  # type: ignore[no-untyped-def]
        observed.append(store._get_sync("session-1"))
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr("roost.snapshot.fs.os.replace", replace_after_peek)
    await store.put("session-1", b"v2-much-longer-content")

    assert observed == [b"v1"]
    assert await store.get("session-1") == b"v2-much-longer-content"
