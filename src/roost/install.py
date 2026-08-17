"""DriverInstaller —— 把 driver 源码注入沙箱并给出启动命令。

契约见 CONTRACTS.md《附录 F — 交付模块》。本模块只承担一类职责：**决定沙箱里
放什么、怎么把 driver 拉起来**；怎么把字节送进沙箱是 SandboxBackend 的事，
沙箱绑定与就绪判定是 sessions.py 的事。

两条设计选择需要解释：

- **收集"已安装的 roost 包源码"而不是维护一份文件清单**：driver 只依赖标准库
  （附录 B 的硬约束），所以"把这个包原样搬进去"就是正确且不会腐化的做法——
  新增模块无需回来改清单。M6 的单 artifact 打包与 fingerprint 会在这层之上做，
  届时 files 的产出方式换实现，启动命令与调用方不受影响。
- **detached 启动经 `sh -c "nohup … &"`**：SandboxBackend.exec 没有 detach 参数
  （附录 E 裁定），后台化因此是调用方约定，写在这里而不是散在编排代码里。

就绪判定**不看这条命令的输出、也不看日志**：唯一权威信号是轮询 `/v1/health`
（sessions.py 负责）。日志重定向到 `/tmp/roost-driver.log` 只为事后排障。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from .protocol import DEFAULT_CONTROL_PORT, ENV_PREFIX

__all__ = [
    "DriverInstaller",
    "DEFAULT_INSTALL_ROOT",
    "DEFAULT_LOG_PATH",
    "DEFAULT_PYTHON",
    "DRIVER_MODULE",
]

DEFAULT_INSTALL_ROOT = "/opt/roost"
DEFAULT_LOG_PATH = "/tmp/roost-driver.log"
DEFAULT_PYTHON = "python"
DEFAULT_BIND_HOST = "0.0.0.0"      # noqa: S104 —— 见 __init__ 的 bind_host 说明
DRIVER_MODULE = "roost.driver"
ENV_PORT = f"{ENV_PREFIX}DRIVER_PORT"
ENV_HOST = f"{ENV_PREFIX}DRIVER_HOST"

_PACKAGE = "roost"
_SOURCE_SUFFIX = ".py"
_SKIP_DIRS = frozenset({"__pycache__"})


class DriverInstaller:
    """把本机已安装的 roost 源码映射成沙箱内文件，并产出 driver 启动命令。

    参数：
        install_root: 沙箱内安装根目录；源码落在 `<root>/src/roost/**`。
        control_port: driver 绑定的 loopback 端口（协议常量，默认 8787）。
        log_path:     driver stdout/stderr 的落盘位置（仅排障用）。
        python:       沙箱内的解释器可执行名。
        bind_host:    driver 在**沙箱内**监听的地址。默认 0.0.0.0 而非 127.0.0.1：
                      docker 的端口发布转发到容器网卡，只听容器内 loopback 的服务
                      从宿主根本不可达。暴露面由 backend 把控（DockerSandboxBackend
                      只把端口发布到宿主 127.0.0.1）；E2B 一类自带代理的 backend
                      可以传回 "127.0.0.1"。
    """

    def __init__(
        self,
        *,
        install_root: str = DEFAULT_INSTALL_ROOT,
        control_port: int = DEFAULT_CONTROL_PORT,
        log_path: str = DEFAULT_LOG_PATH,
        python: str = DEFAULT_PYTHON,
        bind_host: str = DEFAULT_BIND_HOST,
    ) -> None:
        if not install_root.startswith("/"):
            raise ValueError("install_root 必须是沙箱内绝对路径")
        self._install_root = install_root.rstrip("/")
        self._control_port = control_port
        self._log_path = log_path
        self._python = python
        self._bind_host = bind_host
        self._files: dict[str, bytes] | None = None

    # -- 沙箱内布局 -----------------------------------------------------

    @property
    def src_dir(self) -> str:
        """沙箱内的 PYTHONPATH 根（`<install_root>/src`）。"""
        return f"{self._install_root}/src"

    @property
    def package_dir(self) -> str:
        return f"{self.src_dir}/{_PACKAGE}"

    @property
    def control_port(self) -> int:
        return self._control_port

    @property
    def log_path(self) -> str:
        return self._log_path

    # -- 产出 -----------------------------------------------------------

    @property
    def files(self) -> dict[str, bytes]:
        """沙箱内绝对路径 → 源码字节，可直接交给 `SandboxBackend.upload`。"""
        if self._files is None:
            self._files = self._collect()
        return dict(self._files)

    @property
    def env(self) -> dict[str, str]:
        """driver 进程需要的环境变量（启动命令里已内联，导出供观测/复用）。"""
        return {
            ENV_PORT: str(self._control_port),
            ENV_HOST: self._bind_host,
            "PYTHONPATH": self.src_dir,
        }

    def start_command(self) -> list[str]:
        """detached 启动命令（附录 D/E 的 `sh -c "nohup … &"` 约定）。"""
        env = " ".join(f"{key}={value}" for key, value in self.env.items())
        inner = (
            f"nohup env {env} {self._python} -m {DRIVER_MODULE} "
            f">{self._log_path} 2>&1 &"
        )
        return ["sh", "-c", inner]

    def stop_command(self) -> list[str]:
        """停掉沙箱内的 driver 进程（附录 M 的 restart 阶梯第一步）。

        前台执行、有输出：与 start 不同，调用方要等它真的停完才能起新的，
        否则新进程会撞 EADDRINUSE。
        """
        return [self._python, "-m", f"{DRIVER_MODULE}.stop"]

    def probe_command(self) -> list[str]:
        """活性探测命令（附录 M 的 activity probe）。

        脚本住在 roost 包内，因此**已经在 `files` 里**，也就自动计入 runtime
        fingerprint——探测逻辑变了等于运行时变了，沙箱该被判定过期，不需要另建
        一条分发/版本化通道。
        """
        return [self._python, "-m", f"{DRIVER_MODULE}.probe"]

    # -- 内部 -----------------------------------------------------------

    def _collect(self) -> dict[str, bytes]:
        """遍历已安装 roost 包，收集全部 .py 源码。

        整包搬运而非挑模块：driver 需要 driver/ + control/ + events/types/protocol，
        而 `roost/__init__.py` 会把包内其余模块一并 import——挑一半反而进不去。
        全部模块只依赖标准库，因此整包是安全且零维护的选择。
        """
        root = _package_root()
        files: dict[str, bytes] = {}
        for path in sorted(root.rglob(f"*{_SOURCE_SUFFIX}")):
            relative = path.relative_to(root)
            if _SKIP_DIRS.intersection(relative.parts):
                continue
            files[f"{self.package_dir}/{relative.as_posix()}"] = path.read_bytes()
        if not files:
            raise RuntimeError(f"在 {root} 下没有找到任何 roost 源码")
        return files


def _package_root() -> Path:
    """定位已安装 roost 包的源码目录。"""
    spec = importlib.util.find_spec(_PACKAGE)
    locations = list(spec.submodule_search_locations or ()) if spec is not None else []
    if not locations:
        raise RuntimeError("找不到已安装的 roost 包源码目录")
    return Path(locations[0])
