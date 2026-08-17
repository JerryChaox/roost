"""SessionSandboxRegistry —— session 到沙箱的绑定与 cold boot 编排。

契约见 CONTRACTS.md《附录 F — 交付模块》。本模块承担一类职责：**保证"这个
session 现在有一个活着的、装好 driver 的沙箱"**，并把绑定这一事实写回 StateStore。
它不解释 turn、不碰事件流（除了 boot 期的 lifecycle 通告），turn 的收发在 runner.py。

三条语义要点：

- **活性判定只认 `/v1/health`**：有绑定时 `backend.connect` 成功不等于沙箱可用
  （容器可能还在、driver 却已死；附录 E 也明确 connect 对 exited 容器原样返回
  handle）。因此复用路径一律短超时探一次 health，探不通就当死沙箱走 cold boot。
  同理 cold boot 的就绪信号是轮询 health，**不看启动命令的输出或日志**。
- **绝不留未绑定的活容器**：cold boot 中途失败（上传失败、启动失败、就绪超时、
  换绑 CAS 失败）一律 kill 半成品，再把异常抛给调用方。孤儿容器是最贵的一类
  资源泄漏——它不在任何 source of truth 里，没人会来回收。
- **换绑走 CAS**：`swap_binding(old, new)`，old 为读到的旧绑定（无绑定时 None，
  附录 A 预留的"行不存在"分支自此可达）。CAS 失败说明有别的执行者已经换过绑，
  本次 boot 出来的沙箱作废——绝不覆盖别人的绑定。
- **恢复是 boot 的一部分，不是 boot 之后的一步**（I2）：快照在 health 就绪后、
  返回调用方之前灌回工作区，因此"拿到手的沙箱"永远是已经恢复好的。恢复失败按
  boot 失败处理（kill 半成品）——把一个丢了历史的空沙箱交出去，比 boot 失败更糟：
  它会以"一切正常"的样子继续跑，然后在下一个 turn 边界把空工作区备份回去，
  真正的状态就此丢失（备份覆盖恢复源）。
- **runtime 过期走替换而不是原地升级**（M6 / 附录 I）：复用路径命中一个健康沙箱、
  但它的 `RuntimeStamp.runtime_files_hash` 与当前 fingerprint 不符时，本模块
  当场用"取活快照 → cold boot 新沙箱 → CAS 换绑 → 杀旧"把它换掉。失败的每一步
  都退回旧沙箱继续服务本 turn（I3：失败永不伤 turn），并给这个 session 记一段
  backoff，避免每个 turn 都重试一次昂贵的 boot。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from .control import ControlClient, ControlError
from .events import LifecycleNotice
from .fingerprint import runtime_fingerprint
from .install import DriverInstaller
from .ports import (
    EventSink,
    OpsRecorder,
    SandboxBackend,
    SessionContextProvider,
    SnapshotKeyFn,
    SnapshotStore,
    StateStore,
)
from .reducer import reduce_event
from .types import RuntimeStamp, SandboxHandle, SessionBootContext

__all__ = [
    "SessionSandboxRegistry",
    "BootError",
    "BootTimeoutError",
    "BindingConflictError",
    "DEFAULT_BOOT_TIMEOUT",
    "DEFAULT_UPDATE_BACKOFF",
    "SEQ_BOOT_STARTED",
    "SEQ_BOOT_FINISHED",
    "SEQ_UPDATE_STARTED",
    "SEQ_UPDATE_FINISHED",
]

DEFAULT_BOOT_TIMEOUT = 30.0

# forced update 失败后，这个 session 多久之内不再尝试替换（秒）。
DEFAULT_UPDATE_BACKOFF = 1800.0

# boot/update 通告在 display 流保留段里的固定位置（reducer.LIFECYCLE_SEQ_RESERVED）。
SEQ_BOOT_STARTED = 1
SEQ_BOOT_FINISHED = 2
SEQ_UPDATE_STARTED = 3
SEQ_UPDATE_FINISHED = 4

KIND_BOOT_STARTED = "boot_started"
KIND_BOOT_FINISHED = "boot_finished"
KIND_UPDATE_STARTED = "update_started"
KIND_UPDATE_FINISHED = "update_finished"


class BootError(RuntimeError):
    """cold boot 失败；半成品沙箱已被清理。"""


class BootTimeoutError(BootError, TimeoutError):
    """沙箱在 boot_timeout 内没有报告健康。"""


class BindingConflictError(RuntimeError):
    """换绑 CAS 失败：别的执行者已经把 session 绑到了另一个沙箱。"""


class SessionSandboxRegistry:
    """按 session 取得（必要时新建）一个装好 driver 的沙箱。

    参数：
        backend:            SandboxBackend port 实现。
        store:              StateStore port 实现（绑定的 source of truth）。
        installer:          driver 源码与启动命令的产出方。
        context_provider:   宿主的 cold boot 注入物来源（可选）。
        sink:               boot lifecycle 通告的去处（可选）。
        ops:                fire-and-forget 观测（可选）。
        snapshot_store:     工作区快照的来源（可选）。
        snapshot_key:       session_id → 快照 key（可选）。与 snapshot_store 成对
                            出现才启用恢复；缺任一则持久化整体禁用（半套配置
                            按"没配"处理，而不是按"配错了"报错——它可能就是宿主
                            在 M4 之前的既有装配）。
        template:           create 时使用的模板/镜像（None = backend 默认）。
        boot_timeout:       cold boot 到 health 就绪的总时限（秒）。
        health_timeout:     单次 health 探测的超时（秒）——复用路径要"快速失败"。
        poll_interval:      boot 期 health 轮询间隔（秒）。
        request_timeout:    turn 流量用的 ControlClient 请求超时（秒）。
        exec_timeout:       启动命令的 exec 超时（秒）。
        update_backoff:     forced update 失败后，本 session 暂停重试的时长（秒）。
                            0 表示不退避（每个 turn 都重试）。

    **backoff 是进程内状态**（`session_id → 单调时钟到期点`）：进程重启即遗忘，
    多副本宿主之间也不共享，因此最坏情况是每个副本各自浪费一次失败的替换。
    把它做成跨进程的（写 StateStore / 外部 KV）属生产化范围，不在本库的
    source of truth 里——绑定才是。
    """

    def __init__(
        self,
        backend: SandboxBackend,
        store: StateStore,
        *,
        installer: DriverInstaller | None = None,
        context_provider: SessionContextProvider | None = None,
        sink: EventSink | None = None,
        ops: OpsRecorder | None = None,
        snapshot_store: SnapshotStore | None = None,
        snapshot_key: SnapshotKeyFn | None = None,
        template: str | None = None,
        boot_timeout: float = DEFAULT_BOOT_TIMEOUT,
        health_timeout: float = 2.0,
        poll_interval: float = 0.25,
        request_timeout: float = 10.0,
        exec_timeout: float = 60.0,
        update_backoff: float = DEFAULT_UPDATE_BACKOFF,
    ) -> None:
        if boot_timeout <= 0:
            raise ValueError("boot_timeout 必须 > 0")
        if poll_interval <= 0:
            raise ValueError("poll_interval 必须 > 0")
        if update_backoff < 0:
            raise ValueError("update_backoff 必须 >= 0")
        self._backend = backend
        self._store = store
        self._installer = installer if installer is not None else DriverInstaller()
        self._context_provider = context_provider
        self._sink = sink
        self._ops = ops
        self._snapshot_store = snapshot_store
        self._snapshot_key = snapshot_key
        self._template = template
        self._boot_timeout = boot_timeout
        self._health_timeout = health_timeout
        self._poll_interval = poll_interval
        self._request_timeout = request_timeout
        self._exec_timeout = exec_timeout
        self._update_backoff = update_backoff
        self._fingerprint: str | None = None
        self._backoff_until: dict[str, float] = {}

    # -- 入口 -----------------------------------------------------------

    async def get_or_create(
        self, session_id: str, *, turn_id: str = ""
    ) -> tuple[SandboxHandle, ControlClient]:
        """返回该 session 当前可用的沙箱与其控制客户端。

        `turn_id` 只用于给 boot lifecycle 通告归属一个 turn（display 流按 turn 组织）。
        """
        binding = await self._store.get_binding(session_id)
        if binding is not None:
            client = await self._revive(binding)
            if client is not None:
                self._record("sandbox_reused", session_id=session_id,
                             sandbox_id=binding.sandbox_id)
                replaced = await self._maybe_replace(
                    session_id, binding, client, turn_id=turn_id
                )
                return replaced if replaced is not None else (binding, client)
            self._record("sandbox_dead", session_id=session_id,
                         sandbox_id=binding.sandbox_id)

        handle, client = await self._cold_boot(session_id, turn_id=turn_id)
        swapped = await self._store.swap_binding(
            session_id, binding, handle, self._stamp()
        )
        if not swapped:
            await self._kill_quietly(handle)
            raise BindingConflictError(
                f"session {session_id!r} 的绑定在 cold boot 期间被他人改写"
            )
        self._record("sandbox_bound", session_id=session_id,
                     sandbox_id=handle.sandbox_id)
        return handle, client

    async def destroy(self, handle: SandboxHandle) -> None:
        """销毁一个沙箱，**保留绑定行**（附录 H 的停滞收场）。

        绑定继续指向这个死沙箱是有意的：下一次 `get_or_create` 的 health 探测会
        发现它不可用而走 cold boot + 快照恢复，和"沙箱在外部被 docker rm -f 掉"
        完全同一条路径。在这里顺手清绑定反而会多出一种只有本调用制造得出的状态。

        kill 失败不抛：调用方（runner）正在收拾一个已经判定为不可用的沙箱，
        清理失败不该盖掉停滞这个真正的原因；失败经 ops 记 `sandbox_kill_failed`。
        """
        await self._kill_quietly(handle)

    # -- 复用路径 -------------------------------------------------------

    async def _revive(self, binding: SandboxHandle) -> ControlClient | None:
        """连接既有沙箱并探一次 health；不可用返回 None（交由调用方 cold boot）。"""
        try:
            handle = await self._backend.connect(binding.sandbox_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

        probe = self._client(handle, timeout=self._health_timeout)
        try:
            status = await probe.health()
        except asyncio.CancelledError:
            raise
        except (ControlError, OSError, ValueError):
            return None
        if not status.ok:
            return None
        return self._client(handle)

    # -- forced update（M6 / 附录 I） ------------------------------------

    async def _maybe_replace(
        self,
        session_id: str,
        binding: SandboxHandle,
        client: ControlClient,
        *,
        turn_id: str,
    ) -> tuple[SandboxHandle, ControlClient] | None:
        """runtime 过期就把健康的旧沙箱替换掉；不该换或换不成返回 None。

        返回 None 的每一条路径都意味着"调用方拿旧沙箱照常跑这个 turn"——本方法
        除了可能多花一次 boot 的时间之外，**不允许改变 turn 的结果**（I3）。
        """
        stamp = await self._store.get_stamp(session_id)
        # stamp 缺失或 hash 为 None（legacy 绑定 / 快照烘焙）一律豁免比对：M6 不做
        # legacy 迁移，把"不知道版本"当成"过期"会让每个老 session 白挨一次替换。
        if stamp is None or stamp.runtime_files_hash is None:
            return None
        if stamp.runtime_files_hash == self._current_fingerprint():
            return None
        if self._in_backoff(session_id):
            return None

        # 旧沙箱是健康的，它自己就是状态的 source of truth——快照取自它的活内存，
        # 而不是 SnapshotStore（那份可能是上一个 turn 边界的，甚至可能 mis-keyed
        # 而根本不存在；用它恢复等于把一个空工作区当成"恢复成功"交出去）。
        try:
            data = await client.get_workspace()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._deny_updates(session_id)
            self._record("forced_update_aborted", session_id=session_id,
                         sandbox_id=binding.sandbox_id, error=repr(exc))
            return None

        started = time.monotonic()
        try:
            handle, new_client = await self._cold_boot(
                session_id, turn_id=turn_id, announce=False, restore=data
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # _cold_boot 已经 kill 掉半成品；旧沙箱原封不动地继续服务本 turn。
            self._deny_updates(session_id)
            self._record("forced_update_failed", session_id=session_id,
                         sandbox_id=binding.sandbox_id, error=repr(exc))
            return None

        swapped = await self._store.swap_binding(
            session_id, binding, handle, self._stamp()
        )
        if not swapped:
            # 别的执行者已经换过绑：本次 boot 出来的沙箱作废，绝不覆盖别人的绑定。
            await self._kill_quietly(handle)
            self._deny_updates(session_id)
            self._record("forced_update_failed", session_id=session_id,
                         sandbox_id=binding.sandbox_id, new_sandbox_id=handle.sandbox_id,
                         error="binding_conflict")
            return None

        # 换绑成功之后才杀旧沙箱：在此之前它一直是绑定所指、可服务的那一个。
        await self._kill_quietly(binding)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        await self._announce_update(session_id, turn_id, elapsed_ms)
        self._record("forced_update_completed", session_id=session_id,
                     sandbox_id=handle.sandbox_id, old_sandbox_id=binding.sandbox_id,
                     elapsed_ms=elapsed_ms)
        return handle, new_client

    async def _announce_update(
        self, session_id: str, turn_id: str, elapsed_ms: int
    ) -> None:
        """update 通告只在替换成功后发（附录 I：失败路径不发 update 事件）。

        换句话说它是一对"事后"通告而不是进度条：替换失败时旧沙箱照常答题，显示流
        里不该留下任何"正在更新…"的悬空痕迹——那既不是无缝回落，也没有对应的收尾
        事件可发。保留段的 seq 3/4 保证这两条仍排在本 turn 的 driver 事件之前。
        """
        await self._notify(
            session_id, turn_id, KIND_UPDATE_STARTED, 0, SEQ_UPDATE_STARTED
        )
        await self._notify(
            session_id, turn_id, KIND_UPDATE_FINISHED, elapsed_ms, SEQ_UPDATE_FINISHED
        )

    def _current_fingerprint(self) -> str:
        """当前 runtime 的 fingerprint（进程内只算一次：文件表在进程生命期内不变）。"""
        if self._fingerprint is None:
            self._fingerprint = runtime_fingerprint(self._installer)
        return self._fingerprint

    def _in_backoff(self, session_id: str) -> bool:
        deadline = self._backoff_until.get(session_id)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            del self._backoff_until[session_id]   # 过期项就地回收，字典不无限长
            return False
        return True

    def _deny_updates(self, session_id: str) -> None:
        """替换失败后，这个 session 在 backoff 窗口内不再重试。

        失败通常不是一次性的（新版本起不来、快照端点坏了），每个 turn 都重试一次
        cold boot 会把"更新失败"变成"每个 turn 都慢一倍"。
        """
        if self._update_backoff > 0:
            self._backoff_until[session_id] = time.monotonic() + self._update_backoff

    # -- cold boot ------------------------------------------------------

    async def _cold_boot(
        self,
        session_id: str,
        *,
        turn_id: str,
        announce: bool = True,
        restore: bytes | None = None,
    ) -> tuple[SandboxHandle, ControlClient]:
        """boot 一个装好 driver、恢复好工作区的新沙箱。

        两个内部参数只服务于 forced update（附录 I），不属于公开面：`announce`
        关掉 boot 通告（替换期发的是 update 通告，seq 3/4；再发 seq 1/2 会让
        display 流回退），`restore` 用一份现成的字节绕过 SnapshotStore。
        """
        started = time.monotonic()
        if announce:
            await self._notify(
                session_id, turn_id, KIND_BOOT_STARTED, 0, SEQ_BOOT_STARTED
            )
        self._record("sandbox_boot_started", session_id=session_id)

        # 只向宿主索取一次注入物：cold_boot_context 可能是有代价的调用，
        # 而 files/skills（走 upload）与 env（走 exec）必须来自同一份快照。
        context = (
            await self._context_provider.cold_boot_context(session_id)
            if self._context_provider is not None
            else None
        )

        handle = await self._backend.create(template=self._template)
        try:
            await self._backend.upload(handle, self._boot_files(context))
            await self._start_driver(handle, dict(context.env) if context else {})
            client = self._client(handle)
            await self._await_ready(handle, started)
            await self._restore(session_id, client, restore)
        except asyncio.CancelledError:
            await self._kill_quietly(handle)
            raise
        except BaseException as exc:
            await self._kill_quietly(handle)
            self._record("sandbox_boot_failed", session_id=session_id,
                         sandbox_id=handle.sandbox_id, error=repr(exc))
            raise

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if announce:
            await self._notify(
                session_id, turn_id, KIND_BOOT_FINISHED, elapsed_ms, SEQ_BOOT_FINISHED
            )
        self._record("sandbox_boot_finished", session_id=session_id,
                     sandbox_id=handle.sandbox_id, elapsed_ms=elapsed_ms)
        return handle, client

    def _boot_files(self, context: SessionBootContext | None) -> dict[str, bytes]:
        """driver 源码 + 宿主注入物，合成同一次 upload（附录 F）。"""
        files = self._installer.files
        if context is not None:
            files.update(context.files)
            files.update(context.skills)
        return files

    async def _start_driver(
        self, handle: SandboxHandle, env: dict[str, str]
    ) -> None:
        returncode, stdout, stderr = await self._backend.exec(
            handle,
            self._installer.start_command(),
            env=env or None,
            timeout_seconds=self._exec_timeout,
        )
        if returncode != 0:
            raise BootError(
                f"driver 启动命令退出码 {returncode}: {stderr.strip() or stdout.strip()}"
            )

    async def _await_ready(self, handle: SandboxHandle, started: float) -> None:
        """轮询 `/v1/health` 直到就绪或超出 boot_timeout。

        探测失败（连接被拒、超时、非 200）都只是"还没起来"，一律重试到时限；
        唯一的终止条件是就绪或超时——启动是异步的，任何单次失败都不足以定论。
        """
        probe = self._client(handle, timeout=self._health_timeout)
        last: BaseException | None = None
        while time.monotonic() - started < self._boot_timeout:
            try:
                status = await probe.health()
            except asyncio.CancelledError:
                raise
            except (ControlError, OSError, ValueError) as exc:
                last = exc
            else:
                if status.ok:
                    return
                last = None
            await asyncio.sleep(self._poll_interval)
        raise BootTimeoutError(
            f"沙箱 {handle.sandbox_id!r} 在 {self._boot_timeout}s 内未就绪"
            + (f"（最后一次探测：{last!r}）" if last is not None else "")
        )

    async def _restore(
        self, session_id: str, client: ControlClient, override: bytes | None = None
    ) -> None:
        """命中快照就把工作区灌回新沙箱；未命中（首次会话）直接返回。

        失败原样抛出，由 `_cold_boot` 的失败路径 kill 半成品——见模块 docstring
        第四条：交出一个"看起来正常但丢了历史"的沙箱是最坏的结局。

        `override` 是 forced update 从旧沙箱现取的活快照：它比 SnapshotStore 里
        那份新，且**必然属于这个 session**，因此直接覆盖，连查都不查存储层——
        一次 mis-keyed 的 miss 在这条路径上会静悄悄地把状态清零。
        """
        if override is not None:
            await client.put_workspace(override)
            self._record(
                "workspace_restored", session_id=session_id,
                source="live", bytes=len(override),
            )
            return
        if self._snapshot_store is None or self._snapshot_key is None:
            return
        key = self._snapshot_key(session_id)
        data = await self._snapshot_store.get(key)
        if data is None:
            self._record("workspace_restore_missed", session_id=session_id, key=key)
            return
        await client.put_workspace(data)
        self._record(
            "workspace_restored", session_id=session_id, key=key, bytes=len(data)
        )

    # -- 辅助 -----------------------------------------------------------

    def _client(
        self, handle: SandboxHandle, *, timeout: float | None = None
    ) -> ControlClient:
        return ControlClient(
            self._backend,
            handle,
            request_timeout=self._request_timeout if timeout is None else timeout,
        )

    def _stamp(self) -> RuntimeStamp:
        # 写入真实 fingerprint（M6）：这一条绑定的沙箱里装的就是这份文件表，
        # 下一次 get_or_create 拿它和当时的 fingerprint 比对来决定是否替换。
        return RuntimeStamp(
            bound_at=datetime.now(timezone.utc),
            template_id=self._template,
            runtime_files_hash=self._current_fingerprint(),
        )

    async def _kill_quietly(self, handle: SandboxHandle) -> None:
        """清理半成品沙箱；清理失败不得盖掉真正的失败原因。"""
        try:
            await self._backend.kill(handle)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._record("sandbox_kill_failed", sandbox_id=handle.sandbox_id)

    async def _notify(
        self, session_id: str, turn_id: str, kind: str, elapsed_ms: int, seq: int
    ) -> None:
        if self._sink is None:
            return
        notice = LifecycleNotice(
            turn_id=turn_id, kind=kind, elapsed_ms=elapsed_ms, seq=seq
        )
        await self._sink.emit([reduce_event(notice, session_id=session_id)])

    def _record(self, event_type: str, **details: object) -> None:
        if self._ops is None:
            return
        try:
            self._ops.record(event_type, **details)
        except Exception:  # OpsRecorder 契约：绝不 raise——真炸了也不许影响主路径
            pass
