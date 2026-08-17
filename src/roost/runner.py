"""SandboxTurnRunner —— 把一个 turn 送进沙箱、把事件流送回宿主，并在这条流上
   判定"它还活着吗"。

契约见 CONTRACTS.md《附录 F — 交付模块》与《附录 M：M12 生产级 watchdog 语义契约》。
它实现 M1 `TurnProcessor` 的 runner 签名（`async (TurnEnvelope) -> None`），因此在
pipeline 眼里只有两种结局：正常返回 = `finish_turn('finished')`，抛异常 =
`finish_turn('failed')`。

进程模型（M12 的立场）：**语义复刻、进程模型不复刻**。生产系统里的 watcher 是
独立进程；roost 的 watcher 就是这条拉流协程——双时钟与决策矩阵实现在这里，
`watchdog.py` 继续只负责 sweep → requeue。

## 双时钟（替代 M5 的单一 stall_timeout，后者已删除）

- **liveness clock**：driver 侧**任意内部活动**（harness 产出、turn 生命周期转换）
  至今多久。pull 模型里它是 events 响应的元字段 `liveness_quiet_ms`，由 driver
  本地计算——差值跨机传输不需要两端时钟对齐。
- **progress clock**：宿主侧计算，最近一次**可渲染**事件（Delta / ToolEvent）至今
  多久，时间戳一律 clamp 到本 turn 的提交时刻。clamp 不是细节：不 clamp 的话，
  一个空闲了半小时的 session 的下一个 turn 一开跑就"已经安静了半小时"，首轮当场
  被误杀。

单时钟为什么不够，两个方向都有事故：只有 quiet clock 时，一次 90 秒的 API 调用、
一个长 tool、一段 extended thinking 都会被当成卡死杀掉；只有 heartbeat 时，
一个心跳照发、却再也产不出任何可渲染事件的 driver 会被永远当成健康的——那才是
真 hang，由 progress clock 判它的死。

## 决策矩阵（附录 M 逐字复刻）

| liveness | progress | activity probe | 结果 |
|---|---|---|---|
| 新鲜 | 未竭 | — | 继续 watch |
| 新鲜 | 竭 | 即使 ACTIVE | kill/restart（真 hang） |
| 竭 | 未竭 | ACTIVE | silence-defer，继续（计数仅观测，无次数上限） |
| 竭 | 未竭 | 非 ACTIVE | kill/restart |
| 竭 | 竭 | — | kill/restart，threshold_tripped=liveness |

第三行**没有次数上限**是有意的：只要内核说沙箱里还有进程在干活、并且还在出可渲染
事件，就没有理由动它。给 defer 设上限等于给"慢"设上限，而那正是 progress clock
的职责。

## 升级阶梯（driver error ordinal，持久化在 StateStore）

ordinal 1 → **重启沙箱内的 driver 进程**（工作区现场保留，比冷启动便宜得多；
新进程的 registry 是空的，因此把同一个 turn 重新提交给它合法）；ordinal ≥2 →
杀沙箱，走既有的 sweep → requeue 冷启动路径；ordinal ≥6 → 终止 turn
（`finish_turn('failed')` + ops），不再重投。

ordinal **必须持久化**：内存里的 marker 会被一次 requeue 清掉，阶梯就永远停在
第一级——同一个沙箱曾因此被反复 restart 约 95 轮。stall kill 与 driver error
共用这一个计数器。

## 节奏与上限

拉流默认 dense（长轮询 wait_ms）。累计轮次超过 round budget 且仍有活性信号时切到
**long-watch**（间隔放宽到 30s）——round budget 不是 turn 的上限：一个健康的
21 分钟长 turn 曾被它误终止。真正的上限是 wall-clock ceiling（默认 90 分钟），
触顶时**不杀沙箱**，只 `finish_turn('attention')` 并记 ops：需要人工介入，
不是失败，现场留给人看。

## 观测纪律

普通轮次**零写入**（dense / long-watch / 重复 defer 都不写）。允许写入的只有五处：
watch 开始、首次 silence-defer、long-watch 切换（一次）、终结决策、非空 sweep
（最后一处在 watchdog.py）。kill/restart 之前先采一次诊断快照塞进 ops details——
沙箱一旦终止就再也取不到证据了。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import NoReturn

from .backup import BackupCoordinator
from .control.client import DEFAULT_WAIT_MS, MAX_WAIT_MS, ControlClient
from .driver.probe import ProbeResult
from .events import Delta, DriverEvent, Terminal, ToolEvent
from .ports import EventSink, OpsRecorder, StateStore
from .reducer import reduce_events
from .sessions import SessionSandboxRegistry
from .types import SandboxHandle, TurnEnvelope

__all__ = [
    "SandboxTurnRunner",
    "TurnFailedError",
    "TurnStalledError",
    "TurnAbandonedError",
    "TurnNeedsAttentionError",
    "DEFAULT_BOOT_GRACE",
    "DEFAULT_LIVENESS_QUIET",
    "DEFAULT_PROGRESS_QUIET",
    "DEFAULT_FIRST_RENDERABLE",
    "DEFAULT_ROUND_BUDGET",
    "DEFAULT_LONG_WATCH_INTERVAL",
    "DEFAULT_WALL_CLOCK_CEILING",
    "ORDINAL_RESTART",
    "ORDINAL_KILL",
    "ORDINAL_GIVE_UP",
]

# 阈值默认值取生产值（附录 M）。全部 kw-only 构造参数，测试按需调小。
DEFAULT_BOOT_GRACE = 90.0            # 首个事件之前的整体豁免（cold boot 合法静默期）
DEFAULT_LIVENESS_QUIET = 30.0        # driver 任意活动的安静上限
DEFAULT_PROGRESS_QUIET = 180.0       # 可渲染事件的安静上限
DEFAULT_FIRST_RENDERABLE = 30.0      # 首个可渲染事件的期限（自提交时刻起算）
DEFAULT_ROUND_BUDGET = 240           # 切 long-watch 的轮次门槛（不是 turn 上限）
DEFAULT_LONG_WATCH_INTERVAL = 30.0   # long-watch 的拉流间隔
DEFAULT_WALL_CLOCK_CEILING = 90 * 60.0   # 触顶 → attention，不杀沙箱

#: 升级阶梯的三级（附录 M）。
ORDINAL_RESTART = 1     # 重启 driver 进程，保留工作区
ORDINAL_KILL = 2        # 杀沙箱，交给 sweep → requeue
ORDINAL_GIVE_UP = 6     # 终止 turn，不再重投

# 决策矩阵的三种结果。
_CONTINUE = "continue"
_PROBE = "probe"        # 需要活性探测才能定（矩阵第三/四行）
_TRIP = "trip"          # kill/restart

# threshold_tripped 的取值。
_THRESHOLD_PROGRESS = "progress"
_THRESHOLD_LIVENESS = "liveness"


class TurnFailedError(RuntimeError):
    """沙箱里的 turn 以 Terminal(status='error') 结束。"""

    def __init__(self, turn_id: str, error: str | None) -> None:
        super().__init__(f"turn {turn_id!r} 失败：{error or '未提供错误信息'}")
        self.turn_id = turn_id
        self.error = error


class TurnStalledError(TimeoutError):
    """双时钟判定这个 turn 卡死，且阶梯落在"杀沙箱"这一级。

    抛出时沙箱**已被销毁**，绑定行仍指向那个死沙箱（下一次 get_or_create 的
    health 探测自然走 cold boot）。pipeline 见到它既不收尾也不记 failed：锁在
    lock_seconds 内自然过期，watchdog 的 sweep → requeue 是唯一的恢复路径。
    """

    def __init__(self, turn_id: str, threshold_tripped: object = "") -> None:
        super().__init__(
            f"turn {turn_id!r} 停滞（threshold_tripped={threshold_tripped!r}）"
        )
        self.turn_id = turn_id
        self.threshold_tripped = threshold_tripped


class TurnAbandonedError(RuntimeError):
    """error ordinal 触到 `ORDINAL_GIVE_UP`：不再重投。

    runner 在抛之前已经把行写成 'failed'（附录 M）。pipeline 的兜底 finish_turn
    因此是一次 no-op（它只作用于 running 行）——两处写入不会打架。
    """

    def __init__(self, turn_id: str, ordinal: int) -> None:
        super().__init__(f"turn {turn_id!r} 的 error ordinal 到 {ordinal}，终止")
        self.turn_id = turn_id
        self.ordinal = ordinal


class TurnNeedsAttentionError(RuntimeError):
    """wall-clock ceiling 触顶：行写成 'attention'，沙箱**保留**待人工检查。"""

    def __init__(self, turn_id: str, age_seconds: float) -> None:
        super().__init__(f"turn {turn_id!r} 已运行 {age_seconds:.0f}s，需要人工介入")
        self.turn_id = turn_id
        self.age_seconds = age_seconds


@dataclass
class _Watch:
    """一代 watch 的全部状态（一代 = 一个 driver 进程的生命期）。

    `started` 是本代的提交时刻，同时是 progress clock 的 **clamp 下界**：
    last_renderable 初始化成它，因此"上一个 turn 之后的 idle gap"绝不会被算进
    这个 turn 的安静时长（不 clamp 的话，新 turn 的首轮当场被误杀）。

    `turn_started` 是**整个 turn** 的起点，跨代累计：wall-clock ceiling 与
    turn_age_ms 都按它算——restart 一次就把 90 分钟的上限清零，那上限就不存在了。
    """

    started: float
    turn_started: float
    last_renderable: float
    saw_event: bool = False
    saw_renderable: bool = False
    rounds: int = 0
    silence_deferred: int = 0
    long_watch: bool = False
    liveness_quiet_ms: int | None = None
    probe: ProbeResult | None = None
    generation: int = 1
    ordinal: int = 0

    @classmethod
    def begin(cls, now: float, *, turn_started: float, generation: int = 1) -> "_Watch":
        return cls(
            started=now,
            turn_started=turn_started,
            last_renderable=now,
            generation=generation,
        )

    def age_ms(self, now: float) -> int:
        return int((now - self.turn_started) * 1000)

    def progress_quiet_ms(self, now: float) -> int:
        return int(max(0.0, now - self.last_renderable) * 1000)


class SandboxTurnRunner:
    """M1 pipeline 的 runner 实现：沙箱取得 → 提交 turn → 拉事件（并判活）→ 送 sink。

    参数：
        registry:  session 到沙箱的绑定、cold boot、driver 重启与活性探测。
        sink:      DisplayEvent 的去处（EventSink port）。
        store:     StateStore port。**升级阶梯的 ordinal 与终态写入需要它**；
                   传 None 时阶梯降级为"每次判定都杀沙箱"（等价于 M5 的行为），
                   既不会 restart，也不会终止 turn——没有持久计数器时，任何阶梯
                   都只会在重投后从头再来一遍，那比不上一条稳定的恢复路径。
        backup:    turn 边界的工作区备份调度器（可选；None = 不备份）。
        ops:       fire-and-forget 观测（可选）。
        wait_ms:   dense 节奏下单次长轮询的等待上限（毫秒）。
        boot_grace / liveness_quiet / progress_quiet / first_renderable /
        round_budget / long_watch_interval / wall_clock_ceiling：附录 M 的阈值，
        默认取生产值。
    """

    def __init__(
        self,
        registry: SessionSandboxRegistry,
        sink: EventSink,
        *,
        store: StateStore | None = None,
        backup: BackupCoordinator | None = None,
        ops: OpsRecorder | None = None,
        wait_ms: int = DEFAULT_WAIT_MS,
        boot_grace: float = DEFAULT_BOOT_GRACE,
        liveness_quiet: float = DEFAULT_LIVENESS_QUIET,
        progress_quiet: float = DEFAULT_PROGRESS_QUIET,
        first_renderable: float = DEFAULT_FIRST_RENDERABLE,
        round_budget: int = DEFAULT_ROUND_BUDGET,
        long_watch_interval: float = DEFAULT_LONG_WATCH_INTERVAL,
        wall_clock_ceiling: float = DEFAULT_WALL_CLOCK_CEILING,
    ) -> None:
        if wait_ms < 0:
            raise ValueError("wait_ms 必须 >= 0")
        for name, value in (
            ("boot_grace", boot_grace),
            ("liveness_quiet", liveness_quiet),
            ("progress_quiet", progress_quiet),
            ("first_renderable", first_renderable),
            ("long_watch_interval", long_watch_interval),
            ("wall_clock_ceiling", wall_clock_ceiling),
        ):
            if value <= 0:
                raise ValueError(f"{name} 必须 > 0")
        if round_budget < 1:
            raise ValueError("round_budget 必须 >= 1")
        self._registry = registry
        self._sink = sink
        self._store = store
        self._backup = backup
        self._ops = ops
        self._wait_ms = wait_ms
        self._boot_grace = boot_grace
        self._liveness_quiet = liveness_quiet
        self._progress_quiet = progress_quiet
        self._first_renderable = first_renderable
        self._round_budget = round_budget
        self._long_watch_interval = long_watch_interval
        self._wall_clock_ceiling = wall_clock_ceiling

    async def __call__(self, turn: TurnEnvelope) -> None:
        await self.run(turn)

    async def run(self, turn: TurnEnvelope) -> None:
        handle, client = await self._registry.get_or_create(
            turn.session_id, turn_id=turn.turn_id
        )
        client = await self._submit(client, turn, handle, first=True)

        terminal = await self._drain(client, turn, handle)
        if self._backup is not None:
            self._backup.schedule(turn.session_id, client)
        if terminal.status != "ok":
            raise TurnFailedError(turn.turn_id, terminal.error)

    # -- 提交 -----------------------------------------------------------

    async def _submit(
        self,
        client: ControlClient,
        turn: TurnEnvelope,
        handle: SandboxHandle,
        *,
        first: bool,
    ) -> ControlClient:
        """提交 turn 并记一次 watch 开始（这是本轮 watch 允许的唯一一次写入）。"""
        submission = await client.submit_turn(turn)
        self._record(
            "turn_submitted",
            turn_id=turn.turn_id,
            session_id=turn.session_id,
            sandbox_id=handle.sandbox_id,
            state=submission.state,
            turn_state=submission.turn_state,
            after_restart=not first,
        )
        return client

    # -- 事件流与判活 ----------------------------------------------------

    async def _drain(
        self, client: ControlClient, turn: TurnEnvelope, handle: SandboxHandle
    ) -> Terminal:
        """拉流直到 Terminal；期间按决策矩阵判活，必要时走升级阶梯。

        外层循环的一圈 = 一代 watch = 一个 driver 进程：restart 之后 driver 的
        registry 与事件缓存都是空的，cursor 因此从 0 重来，两个时钟也重新起算。
        """
        turn_started = time.monotonic()
        generation = 1
        while True:
            watch = _Watch.begin(
                time.monotonic(), turn_started=turn_started, generation=generation
            )
            outcome = await self._watch(client, turn, handle, watch)
            if isinstance(outcome, Terminal):
                return outcome
            # outcome is None：阶梯判定要重启 driver，换一个 client 重来一代。
            client = await self._restart(turn, handle, watch)
            await self._submit(client, turn, handle, first=False)
            generation += 1

    async def _watch(
        self,
        client: ControlClient,
        turn: TurnEnvelope,
        handle: SandboxHandle,
        watch: _Watch,
    ) -> Terminal | None:
        """一代 watch。返回 Terminal 表示 turn 结束；返回 None 表示"请重启 driver"。

        其余的终结路径（杀沙箱 / 终止 turn / attention）都以抛异常收场。
        """
        cursor = 0
        while True:
            now = time.monotonic()
            if now - watch.turn_started >= self._wall_clock_ceiling:
                await self._attention(turn, handle, watch, now)

            page = await client.fetch_events(
                turn.turn_id, after=cursor, wait_ms=self._page_wait_ms(watch)
            )
            cursor = page.next_after
            watch.rounds += 1
            watch.liveness_quiet_ms = page.liveness_quiet_ms

            if page.events:
                watch.saw_event = True
                if _has_renderable(page.events):
                    watch.saw_renderable = True
                    watch.last_renderable = time.monotonic()
                await self._sink.emit(
                    reduce_events(page.events, session_id=turn.session_id)
                )
                terminal = _terminal_of(page.events)
                if terminal is not None:
                    return terminal
                # 出着事件的流不切 long-watch：节奏放慢是给"安静但活着"的场面用的，
                # 对一条正在流式输出的 turn 放慢拉流只会让渲染变卡。
                continue

            verdict, threshold = self._decide(watch, time.monotonic())
            if verdict == _PROBE:
                watch.probe = await self._registry.probe_activity(handle)
                if watch.probe.active:
                    self._defer(turn, handle, watch)          # 矩阵第三行
                    self._maybe_long_watch(turn, watch)
                    continue
                verdict, threshold = _TRIP, _THRESHOLD_LIVENESS   # 矩阵第四行
            if verdict == _TRIP:
                # _escalate 的其余两级都以抛异常收场；能返回就只有"重启"这一种。
                await self._escalate(turn, handle, watch, threshold)
                return None
            self._maybe_long_watch(turn, watch)

    def _page_wait_ms(self, watch: _Watch) -> int:
        """本轮长轮询的等待上限：dense 用 wait_ms，long-watch 放宽到间隔。"""
        if not watch.long_watch:
            return self._wait_ms
        return min(int(self._long_watch_interval * 1000), MAX_WAIT_MS)

    def _decide(self, watch: _Watch, now: float) -> tuple[str, str]:
        """决策矩阵（纯判定，不做 IO、不写观测）。

        boot grace 先于矩阵：**第一个事件到来之前**整段判定豁免。cold boot 期间
        沙箱合法地什么都不产出，早年按这里判死引发过 reload 风暴。豁免只看
        "有没有过任何事件"，不看是不是可渲染——首个可渲染事件的期限
        （first_renderable）是 grace 之后才生效的下限。
        """
        if not watch.saw_event and now - watch.started < self._boot_grace:
            return _CONTINUE, ""

        progress_budget = (
            self._progress_quiet if watch.saw_renderable else self._first_renderable
        )
        progress_exhausted = now - watch.last_renderable >= progress_budget
        # liveness_quiet_ms 缺失 = driver 不支持这个字段（老 runtime）。此时
        # liveness 一律按"新鲜"处理：只用 progress clock 判定，退化成单时钟，
        # 而不是把每个老沙箱当场判死。
        liveness_exhausted = (
            watch.liveness_quiet_ms is not None
            and watch.liveness_quiet_ms >= self._liveness_quiet * 1000
        )

        if not liveness_exhausted:
            # 第一行 / 第二行：progress 竭就是真 hang——心跳新鲜也照杀。
            return (_TRIP, _THRESHOLD_PROGRESS) if progress_exhausted else (_CONTINUE, "")
        if progress_exhausted:
            return _TRIP, _THRESHOLD_LIVENESS        # 第五行
        return _PROBE, ""                            # 第三/四行：交给探测

    # -- 观测（只在允许写入的时刻） ---------------------------------------

    def _defer(self, turn: TurnEnvelope, handle: SandboxHandle, watch: _Watch) -> None:
        """silence-defer：计数恒增，**只有第一次**写 ops（重复 defer 零写入）。"""
        watch.silence_deferred += 1
        if watch.silence_deferred != 1:
            return
        self._record(
            "watch_silence_deferred",
            **self._fields(turn, handle, watch, threshold_tripped=""),
        )

    def _maybe_long_watch(self, turn: TurnEnvelope, watch: _Watch) -> None:
        """轮次超预算且仍有活性信号 → 切 long-watch 节奏，**只写一次**。

        round budget 不是 turn 的上限：健康的 21 分钟长 turn 曾被它误终止。
        走到这里说明矩阵刚判了"继续"，即活性信号仍在——要做的只是把拉流放慢。
        """
        if watch.long_watch or watch.rounds <= self._round_budget:
            return
        watch.long_watch = True
        self._record(
            "watch_long_watch_started",
            turn_id=turn.turn_id,
            session_id=turn.session_id,
            watch_round=watch.rounds,
            interval_ms=int(self._long_watch_interval * 1000),
        )

    def _fields(
        self,
        turn: TurnEnvelope,
        handle: SandboxHandle,
        watch: _Watch,
        *,
        threshold_tripped: str,
    ) -> dict:
        """附录 M 钉死的观测字段（每条稀疏事件都带同一组，便于串起一条时间线）。"""
        now = time.monotonic()
        details = {
            "turn_id": turn.turn_id,
            "session_id": turn.session_id,
            "sandbox_id": handle.sandbox_id,
            "watch_round": watch.rounds,
            "threshold_tripped": threshold_tripped,
            "liveness_quiet_ms": watch.liveness_quiet_ms,
            "progress_quiet_ms": watch.progress_quiet_ms(now),
            "turn_age_ms": watch.age_ms(now),
            "silence_deferred_count": watch.silence_deferred,
            "reload_generation": watch.generation,
            "error_ordinal": watch.ordinal,
        }
        if watch.probe is not None:
            details["probe_active"] = watch.probe.active
            details["probe_reason"] = watch.probe.reason
            # 诊断快照：沙箱一旦终止，/proc 的现场就永远取不到了。截断到 20 条——
            # 要的是"当时谁在跑"，不是一份完整的进程表。
            details["probe_processes"] = watch.probe.processes[:20]
        return details

    # -- 升级阶梯 -------------------------------------------------------

    async def _escalate(
        self,
        turn: TurnEnvelope,
        handle: SandboxHandle,
        watch: _Watch,
        threshold: str,
    ) -> bool:
        """判定为卡死之后走阶梯。返回 True 表示"已重启，请重来一代"；其余路径抛异常。

        诊断快照先于一切动作：沙箱一旦被终止，`/proc` 的现场就永远取不到了，
        而"当时到底是谁在跑"往往是唯一能解释这次 kill 的证据。
        """
        if watch.probe is None:
            watch.probe = await self._registry.probe_activity(handle)
        watch.ordinal = await self._bump_ordinal(turn.turn_id)

        if watch.ordinal >= ORDINAL_GIVE_UP:
            await self._abandon(turn, handle, watch, threshold)
        if watch.ordinal <= ORDINAL_RESTART:
            self._record(
                "driver_restart_requested",
                **self._fields(turn, handle, watch, threshold_tripped=threshold),
            )
            return True
        await self._kill(turn, handle, watch, threshold)

    async def _bump_ordinal(self, turn_id: str) -> int:
        """持久化的阶梯计数。没有 store 时固定返回 `ORDINAL_KILL`（见构造参数说明）。"""
        if self._store is None:
            return ORDINAL_KILL
        return await self._store.bump_error_ordinal(turn_id)

    async def _restart(
        self, turn: TurnEnvelope, handle: SandboxHandle, watch: _Watch
    ) -> ControlClient:
        """阶梯第一级：重启 driver 进程。失败就地升级到杀沙箱。"""
        try:
            return await self._registry.restart_driver(turn.session_id, handle)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record(
                "driver_restart_failed",
                error=repr(exc),
                **self._fields(turn, handle, watch, threshold_tripped=""),
            )
            await self._kill(turn, handle, watch, _THRESHOLD_LIVENESS)

    async def _kill(
        self,
        turn: TurnEnvelope,
        handle: SandboxHandle,
        watch: _Watch,
        threshold: str,
    ) -> NoReturn:
        """阶梯第二级：销毁沙箱 → raise。恢复交给 sweep → requeue（唯一路径）。

        刻意**不备份**工作区：要备份就得再向这个已经判定为不响应的 driver 发一次
        请求。停滞 turn 的工作区回到上一次成功 turn 的快照，是比"卡在半路又拖住
        恢复"更好的起点。
        """
        await self._registry.destroy(handle)
        self._record(
            "sandbox_stalled_killed",
            **self._fields(turn, handle, watch, threshold_tripped=threshold),
        )
        raise TurnStalledError(turn.turn_id, threshold)

    async def _abandon(
        self,
        turn: TurnEnvelope,
        handle: SandboxHandle,
        watch: _Watch,
        threshold: str,
    ) -> NoReturn:
        """阶梯第三级：终止 turn，不再重投。

        先把行写成 'failed' 再抛：重投的唯一入口是 sweep，而 sweep 只看
        running/requeued——落成终态就是"不再重投"这件事的表达方式。沙箱照杀
        （它已经被判定为不可用），只是没有人会再来接这个 turn。
        """
        await self._registry.destroy(handle)
        if self._store is not None:
            await self._store.finish_turn(turn.turn_id, status="failed")
        self._record(
            "turn_abandoned",
            **self._fields(turn, handle, watch, threshold_tripped=threshold),
        )
        raise TurnAbandonedError(turn.turn_id, watch.ordinal)

    async def _attention(
        self, turn: TurnEnvelope, handle: SandboxHandle, watch: _Watch, now: float
    ) -> NoReturn:
        """wall-clock ceiling：**不杀沙箱**，行落 'attention'。

        这是与 failed 不同的一件事：turn 一直在正常出事件、只是没完没了，沙箱
        大概率还好好的。杀掉它等于销毁唯一的现场，而重投一个已经跑了 90 分钟的
        turn 只会再跑 90 分钟。留着，让人进去看。
        """
        if self._store is not None:
            await self._store.finish_turn(turn.turn_id, status="attention")
        self._record(
            "turn_attention",
            **self._fields(turn, handle, watch, threshold_tripped="wall_clock"),
        )
        raise TurnNeedsAttentionError(turn.turn_id, now - watch.started)

    def _record(self, event_type: str, **details: object) -> None:
        if self._ops is None:
            return
        try:
            self._ops.record(event_type, **details)
        except Exception:  # OpsRecorder 契约：绝不 raise
            pass


def _terminal_of(events: list[DriverEvent]) -> Terminal | None:
    """取本批里的 Terminal（driver 不变量：恒为整个流的最后一条）。"""
    for event in events:
        if isinstance(event, Terminal):
            return event
    return None


def _has_renderable(events: list[DriverEvent]) -> bool:
    """progress clock 只认**可渲染**事件：Delta 与 ToolEvent。

    lifecycle notice 与 terminal 刻意不算：前者是"库自己在忙"（boot/update），
    后者是终点。一个只会发 lifecycle 的 driver 恰恰是要判死的那种。
    """
    return any(isinstance(event, (Delta, ToolEvent)) for event in events)
