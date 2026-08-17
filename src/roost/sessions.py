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
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from .control import ControlClient, ControlError
from .events import LifecycleNotice
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
    "SEQ_BOOT_STARTED",
    "SEQ_BOOT_FINISHED",
]

DEFAULT_BOOT_TIMEOUT = 30.0

# boot 通告在 display 流保留段里的固定位置（reducer.LIFECYCLE_SEQ_RESERVED）。
SEQ_BOOT_STARTED = 1
SEQ_BOOT_FINISHED = 2

KIND_BOOT_STARTED = "boot_started"
KIND_BOOT_FINISHED = "boot_finished"


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
    ) -> None:
        if boot_timeout <= 0:
            raise ValueError("boot_timeout 必须 > 0")
        if poll_interval <= 0:
            raise ValueError("poll_interval 必须 > 0")
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
                return binding, client
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

    # -- cold boot ------------------------------------------------------

    async def _cold_boot(
        self, session_id: str, *, turn_id: str
    ) -> tuple[SandboxHandle, ControlClient]:
        started = time.monotonic()
        await self._notify(session_id, turn_id, KIND_BOOT_STARTED, 0, SEQ_BOOT_STARTED)
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
            await self._restore(session_id, client)
        except asyncio.CancelledError:
            await self._kill_quietly(handle)
            raise
        except BaseException as exc:
            await self._kill_quietly(handle)
            self._record("sandbox_boot_failed", session_id=session_id,
                         sandbox_id=handle.sandbox_id, error=repr(exc))
            raise

        elapsed_ms = int((time.monotonic() - started) * 1000)
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

    async def _restore(self, session_id: str, client: ControlClient) -> None:
        """命中快照就把工作区灌回新沙箱；未命中（首次会话）直接返回。

        失败原样抛出，由 `_cold_boot` 的失败路径 kill 半成品——见模块 docstring
        第四条：交出一个"看起来正常但丢了历史"的沙箱是最坏的结局。
        """
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
        # runtime_files_hash 留 None：fingerprint 与比对语义归 M6 forced update，
        # 在那之前"豁免比对"正是契约给 None 的含义（types.py）。
        return RuntimeStamp(
            bound_at=datetime.now(timezone.utc),
            template_id=self._template,
            runtime_files_hash=None,
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
