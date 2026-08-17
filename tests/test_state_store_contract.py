"""StateStore 契约套件。

只依赖 StateStore 的 port 面，覆盖 CONTRACTS.md 附录 A《语义钉死》的每一条。
通过 conftest 的 `state_store` 参数化夹具，本套件约束**所有**现在与未来的实现。

防的回归：CAS 与锁语义一旦松动，I1（每条消息恰好一次执行）与 sweep 恢复路径
会同时失效，且失效表现是静默重跑/静默丢件，靠类型检查与 lint 都发现不了。
"""

from __future__ import annotations

import pytest
from conftest import FakeClock, make_handle, make_stamp, make_turn

LOCK = 30


# ---- begin_turn 四种返回情形 -------------------------------------------------


async def test_begin_turn_inserts_when_row_absent(state_store) -> None:
    assert await state_store.begin_turn(make_turn(), lock_seconds=LOCK) is True


async def test_begin_turn_false_when_running(state_store) -> None:
    turn = make_turn()
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is True
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is False


async def test_begin_turn_false_when_running_lock_expired(
    state_store, clock: FakeClock
) -> None:
    """过期锁的接管只走 sweep→requeue，begin_turn 自身绝不抢锁。"""
    turn = make_turn()
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is True
    clock.advance(LOCK + 1)
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is False


async def test_begin_turn_takes_over_requeued_with_monotonic_attempt(
    state_store, clock: FakeClock
) -> None:
    turn = make_turn(attempt=1)
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is True
    clock.advance(LOCK + 1)
    assert [t.turn_id for t in await state_store.sweep_due_turns(limit=10)] == [
        turn.turn_id
    ]

    # 投递方是 attempt 的唯一 +1 所有者：重投的 envelope 带 attempt=2，
    # 接管取 MAX(行值, envelope 值)，行与 envelope 一致、不双计。
    from dataclasses import replace

    assert await state_store.begin_turn(replace(turn, attempt=2), lock_seconds=LOCK) is True
    clock.advance(LOCK + 1)
    swept = await state_store.sweep_due_turns(limit=10)
    assert [t.attempt for t in swept] == [2]

    # 迟到的旧 envelope（attempt=1）接管时不使 attempt 回退。
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is True
    clock.advance(LOCK + 1)
    assert [t.attempt for t in await state_store.sweep_due_turns(limit=10)] == [2]


async def test_finish_turn_rejects_non_terminal_status(state_store) -> None:
    turn = make_turn()
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is True
    with pytest.raises(ValueError):
        await state_store.finish_turn(turn.turn_id, status="requeued")


async def test_begin_turn_false_when_finished(state_store) -> None:
    turn = make_turn()
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is True
    await state_store.finish_turn(turn.turn_id, status="finished")
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is False


async def test_begin_turn_false_when_failed(state_store) -> None:
    turn = make_turn()
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is True
    await state_store.finish_turn(turn.turn_id, status="failed")
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is False


# ---- sweep_due_turns ---------------------------------------------------------


async def test_sweep_returns_expired_running_with_original_attempt(
    state_store, clock: FakeClock
) -> None:
    turn = make_turn(payload={"text": "sweep me"}, context={"k": "v"}, attempt=3)
    await state_store.begin_turn(turn, lock_seconds=LOCK)
    clock.advance(LOCK + 1)

    swept = await state_store.sweep_due_turns(limit=10)

    assert len(swept) == 1
    recovered = swept[0]
    assert recovered.turn_id == turn.turn_id
    assert recovered.session_id == turn.session_id
    assert recovered.payload == {"text": "sweep me"}
    assert recovered.context == {"k": "v"}
    # attempt 用行内原值；+1 由投递方重投时负责。
    assert recovered.attempt == 3


async def test_sweep_skips_unexpired_and_is_not_repeatable(
    state_store, clock: FakeClock
) -> None:
    fresh = make_turn("turn-fresh")
    stale = make_turn("turn-stale", session_id="session-2")
    await state_store.begin_turn(stale, lock_seconds=LOCK)
    clock.advance(LOCK + 1)
    await state_store.begin_turn(fresh, lock_seconds=LOCK)

    assert [t.turn_id for t in await state_store.sweep_due_turns(limit=10)] == [
        "turn-stale"
    ]
    # 已转 requeued 的行不会被第二次扫出来（否则 watchdog 会重复认领）。
    assert await state_store.sweep_due_turns(limit=10) == []


async def test_sweep_respects_limit(state_store, clock: FakeClock) -> None:
    for index in range(3):
        await state_store.begin_turn(
            make_turn(f"turn-{index}", session_id=f"session-{index}"),
            lock_seconds=LOCK,
        )
    clock.advance(LOCK + 1)

    assert len(await state_store.sweep_due_turns(limit=2)) == 2
    assert len(await state_store.sweep_due_turns(limit=2)) == 1


# ---- has_active_turn ---------------------------------------------------------


async def test_has_active_turn_true_while_locked(state_store) -> None:
    turn = make_turn()
    await state_store.begin_turn(turn, lock_seconds=LOCK)
    assert await state_store.has_active_turn(turn.session_id) is True


async def test_has_active_turn_excludes_given_turn(state_store) -> None:
    turn = make_turn()
    await state_store.begin_turn(turn, lock_seconds=LOCK)
    assert (
        await state_store.has_active_turn(
            turn.session_id, exclude_turn_id=turn.turn_id
        )
        is False
    )


async def test_has_active_turn_false_when_lock_expired(
    state_store, clock: FakeClock
) -> None:
    """锁过期的 running 行不算 active——wedged turn 不应永久堵死 session。"""
    turn = make_turn()
    await state_store.begin_turn(turn, lock_seconds=LOCK)
    clock.advance(LOCK + 1)
    assert await state_store.has_active_turn(turn.session_id) is False


async def test_has_active_turn_false_for_other_session(state_store) -> None:
    await state_store.begin_turn(make_turn(), lock_seconds=LOCK)
    assert await state_store.has_active_turn("session-other") is False


# ---- renew / finish 仅作用于 running ------------------------------------------


async def test_renew_extends_lock_of_running_turn(
    state_store, clock: FakeClock
) -> None:
    turn = make_turn()
    await state_store.begin_turn(turn, lock_seconds=LOCK)
    clock.advance(LOCK - 1)
    await state_store.renew_turn_lock(turn.turn_id, lock_seconds=LOCK)
    clock.advance(2)

    # 续锁生效：本该过期的 turn 仍是 active，且扫不出来。
    assert await state_store.has_active_turn(turn.session_id) is True
    assert await state_store.sweep_due_turns(limit=10) == []


async def test_renew_is_noop_for_requeued_turn(state_store, clock: FakeClock) -> None:
    turn = make_turn()
    await state_store.begin_turn(turn, lock_seconds=LOCK)
    clock.advance(LOCK + 1)
    await state_store.sweep_due_turns(limit=10)

    await state_store.renew_turn_lock(turn.turn_id, lock_seconds=LOCK)

    # 未被复活成 running：既不算 active，也仍可被 begin_turn 接管。
    assert await state_store.has_active_turn(turn.session_id) is False
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is True


async def test_finish_is_noop_for_requeued_turn(state_store, clock: FakeClock) -> None:
    turn = make_turn()
    await state_store.begin_turn(turn, lock_seconds=LOCK)
    clock.advance(LOCK + 1)
    await state_store.sweep_due_turns(limit=10)

    await state_store.finish_turn(turn.turn_id, status="finished")

    # 没被 finish 掉：仍停留在 requeued，可被接管重跑。
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is True


async def test_finish_is_noop_for_unknown_turn(state_store) -> None:
    await state_store.finish_turn("turn-does-not-exist", status="finished")
    await state_store.renew_turn_lock("turn-does-not-exist", lock_seconds=LOCK)


async def test_finished_turn_is_never_swept(state_store, clock: FakeClock) -> None:
    turn = make_turn()
    await state_store.begin_turn(turn, lock_seconds=LOCK)
    await state_store.finish_turn(turn.turn_id, status="finished")
    clock.advance(LOCK + 1)
    assert await state_store.sweep_due_turns(limit=10) == []


# ---- 绑定与 CAS --------------------------------------------------------------


async def test_binding_roundtrip(state_store) -> None:
    stamp = make_stamp()
    handle = make_handle()
    assert await state_store.get_binding("session-1") is None
    assert await state_store.get_stamp("session-1") is None

    await state_store.bind("session-1", handle, stamp)

    assert await state_store.get_binding("session-1") == handle
    assert await state_store.get_stamp("session-1") == stamp


async def test_binding_roundtrip_with_null_stamp_fields(state_store) -> None:
    """runtime_files_hash=None 表示快照构建期烘焙，必须能原样往返。"""
    stamp = make_stamp(template_id=None, runtime_files_hash=None)
    await state_store.bind("session-1", make_handle(), stamp)
    assert await state_store.get_stamp("session-1") == stamp


async def test_bind_overwrites_existing_binding(state_store) -> None:
    await state_store.bind("session-1", make_handle("sbx-1"), make_stamp())
    await state_store.bind("session-1", make_handle("sbx-2"), make_stamp())
    assert (await state_store.get_binding("session-1")).sandbox_id == "sbx-2"


async def test_swap_binding_succeeds_from_unbound(state_store) -> None:
    new = make_handle("sbx-new")
    assert await state_store.swap_binding("session-1", None, new, make_stamp()) is True
    assert await state_store.get_binding("session-1") == new


async def test_swap_binding_rejects_none_when_already_bound(state_store) -> None:
    current = make_handle("sbx-1")
    await state_store.bind("session-1", current, make_stamp())

    assert (
        await state_store.swap_binding(
            "session-1", None, make_handle("sbx-new"), make_stamp()
        )
        is False
    )
    assert await state_store.get_binding("session-1") == current


async def test_swap_binding_rejects_stale_expectation_without_side_effects(
    state_store,
) -> None:
    current = make_handle("sbx-1")
    stamp = make_stamp(template_id="tpl-1")
    await state_store.bind("session-1", current, stamp)

    swapped = await state_store.swap_binding(
        "session-1",
        make_handle("sbx-stale"),
        make_handle("sbx-new"),
        make_stamp(template_id="tpl-new"),
    )

    assert swapped is False
    # CAS 失败不得留下任何写入副作用——binding 与 stamp 都必须原封不动。
    assert await state_store.get_binding("session-1") == current
    assert await state_store.get_stamp("session-1") == stamp


async def test_swap_binding_rejects_backend_mismatch(state_store) -> None:
    current = make_handle("sbx-1", backend="docker")
    await state_store.bind("session-1", current, make_stamp())

    swapped = await state_store.swap_binding(
        "session-1",
        make_handle("sbx-1", backend="e2b"),
        make_handle("sbx-new"),
        make_stamp(),
    )

    assert swapped is False
    assert await state_store.get_binding("session-1") == current


async def test_swap_binding_succeeds_on_exact_match(state_store) -> None:
    current = make_handle("sbx-1")
    new = make_handle("sbx-2")
    new_stamp = make_stamp(template_id="tpl-2", runtime_files_hash="sha-2")
    await state_store.bind("session-1", current, make_stamp())

    assert await state_store.swap_binding("session-1", current, new, new_stamp) is True
    assert await state_store.get_binding("session-1") == new
    assert await state_store.get_stamp("session-1") == new_stamp


async def test_sweep_stranded_requeued_recovers_after_grace(
    state_store, clock: FakeClock
) -> None:
    """sweep 与重投之间没有原子性：requeued 行若因 enqueue 失败/宿主崩溃而搁浅，
    必须在重投期限（redelivery grace）到期后被再次扫出，不得静默失声。"""
    turn = make_turn()
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is True
    clock.advance(LOCK + 1)
    assert [t.turn_id for t in await state_store.sweep_due_turns(limit=10)] == [
        turn.turn_id
    ]
    # 期限内不重复认领。
    assert await state_store.sweep_due_turns(limit=10) == []
    # 期限过后搁浅行自愈：再次被扫出，且仍可被 begin_turn 正常接管。
    clock.advance(LOCK + 1)
    assert [t.turn_id for t in await state_store.sweep_due_turns(limit=10)] == [
        turn.turn_id
    ]
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is True


# ---- error ordinal 与 attention 终态（附录 M） --------------------------------


async def test_bump_error_ordinal_starts_at_one_and_increments(state_store) -> None:
    """自增后的值原样返回：阶梯的每一级只走一次，靠的就是这个返回值。"""
    turn = make_turn()
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is True
    assert await state_store.bump_error_ordinal(turn.turn_id) == 1
    assert await state_store.bump_error_ordinal(turn.turn_id) == 2
    assert await state_store.bump_error_ordinal(turn.turn_id) == 3


async def test_error_ordinal_survives_requeue_and_takeover(
    state_store, clock: FakeClock
) -> None:
    """**这条是附录 M 的事故本体**：ordinal 若随重投复位，阶梯永远停在第一级，
    同一个沙箱会被反复 restart（生产里数到过约 95 轮）。因此 sweep 转 requeued、
    begin_turn 接管都不得碰它。"""
    turn = make_turn()
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is True
    assert await state_store.bump_error_ordinal(turn.turn_id) == 1

    clock.advance(LOCK + 1)
    assert [t.turn_id for t in await state_store.sweep_due_turns(limit=10)] == [
        turn.turn_id
    ]
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is True

    assert await state_store.bump_error_ordinal(turn.turn_id) == 2


async def test_bump_error_ordinal_is_zero_for_unknown_turn(state_store) -> None:
    """行不存在时没有可自增的东西——返回 0，不建行、不抛。"""
    assert await state_store.bump_error_ordinal("nope") == 0


async def test_finish_turn_accepts_attention(state_store) -> None:
    """'attention'（附录 M 对附录 A 词表的修订）是终态：写得进，且从此不再被扫出。"""
    turn = make_turn()
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is True
    await state_store.finish_turn(turn.turn_id, status="attention")

    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is False


async def test_attention_turn_is_never_swept(state_store, clock: FakeClock) -> None:
    turn = make_turn()
    assert await state_store.begin_turn(turn, lock_seconds=LOCK) is True
    await state_store.finish_turn(turn.turn_id, status="attention")
    clock.advance(LOCK * 10)
    assert await state_store.sweep_due_turns(limit=10) == []
