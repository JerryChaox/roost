"""E2BSandboxBackend 的不依赖凭据的单测（附录 J 的"常规单测"那一半）。

防的回归是三件与云无关、但一坏就全线坏的事：

- **可选依赖的失败面**：没装 extra 时必须是带安装指引的 `MissingDependencyError`，
  而不是从库深处漏出来的 ImportError；且 `import roost` 本身不许因此失败。
- **参数/URL 组装**：argv → 命令字符串、控制端口 → HTTPS origin、traffic token
  → 请求 header。这三处是 backend 与 E2B 之间的全部约定，写错了云上才会发现。
- **凭据解析顺序**：构造参数 > `ROOST_E2B_API_KEY`。断言的是**取到哪个来源**，
  不是 key 的值。

不测 SDK 自己的行为（那是 test_e2b_backend.py 对真实 E2B 的事）。
"""

from __future__ import annotations

import pytest

from roost.backends import MissingDependencyError
from roost.backends.e2b import BACKEND_NAME, DEFAULT_BIND_HOST, E2BSandboxBackend
from roost.backends.e2b_sdk import ENV_API_KEY, INSTALL_HINT, E2BSdk


class FakeSandbox:
    def __init__(self, sandbox_id: str = "sbx-1", token: str | None = "tok") -> None:
        self.sandbox_id = sandbox_id
        self.traffic_access_token = token

    def get_host(self, port: int) -> str:
        return f"{port}-{self.sandbox_id}.e2b.app"


class FakeSdk:
    """E2BSdk 的调用面替身：记录调用，不碰网络。"""

    def __init__(self, sandbox: FakeSandbox | None = None) -> None:
        self.sandbox = sandbox or FakeSandbox()
        self.calls: list[tuple] = []

    def ensure_loaded(self) -> None:
        return None

    async def create(self, template):
        self.calls.append(("create", template))
        return self.sandbox

    async def connect(self, sandbox_id):
        self.calls.append(("connect", sandbox_id))
        return self.sandbox

    async def pause(self, sandbox):
        self.calls.append(("pause", sandbox.sandbox_id))
        return True

    async def kill(self, sandbox):
        self.calls.append(("kill", sandbox.sandbox_id))
        return True

    async def write_files(self, sandbox, files):
        self.calls.append(("write_files", dict(files)))

    async def run(self, sandbox, command, *, envs=None, timeout_seconds=None):
        self.calls.append(("run", command, envs, timeout_seconds))
        return 0, "out", ""

    def origin(self, sandbox, port):
        return E2BSdk.origin(self, sandbox, port)  # 复用真实实现，只是不联网

    traffic_headers = staticmethod(E2BSdk.traffic_headers)


# -- 可选依赖 -----------------------------------------------------------


def test_missing_sdk_raises_actionable_error() -> None:
    def loader():
        raise MissingDependencyError(f"e2b 未安装。安装：{INSTALL_HINT}")

    sdk = E2BSdk(loader=loader)
    with pytest.raises(MissingDependencyError) as excinfo:
        _ = sdk.module
    assert INSTALL_HINT in str(excinfo.value)


def test_backend_instantiation_fails_when_extra_missing() -> None:
    """附录 J：报错时机是**实例化**，不是编排半途第一次 create。"""
    def loader():
        raise MissingDependencyError(f"e2b 未安装。安装：{INSTALL_HINT}")

    with pytest.raises(MissingDependencyError) as excinfo:
        E2BSandboxBackend(sdk=E2BSdk(loader=loader))
    assert INSTALL_HINT in str(excinfo.value)


def test_importing_roost_does_not_import_e2b_sdk() -> None:
    """顶层导出是惰性的：拿到名字之前，不该有任何 e2b SDK 被 import。"""
    import subprocess
    import sys

    code = (
        "import sys; import roost; "
        "assert 'e2b' not in sys.modules, sorted(m for m in sys.modules if 'e2b' in m); "
        "assert 'roost.backends.e2b' not in sys.modules; "
        "print(roost.E2BSandboxBackend.__name__)"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "E2BSandboxBackend"


# -- 凭据解析 -----------------------------------------------------------


def test_api_key_prefers_constructor_then_env(monkeypatch) -> None:
    monkeypatch.setenv(ENV_API_KEY, "from-env")
    assert E2BSdk(api_key="from-arg").api_key() == "from-arg"
    assert E2BSdk().api_key() == "from-env"
    monkeypatch.delenv(ENV_API_KEY)
    # 都没有 → None，意味着"交给 SDK 自己解析"，而不是拼一个空 key 过去。
    assert E2BSdk().api_key() is None


# -- 参数与 URL 组装 -----------------------------------------------------


async def test_create_uses_configured_template_and_marks_handle() -> None:
    sdk = FakeSdk()
    backend = E2BSandboxBackend(template="tmpl-x", sdk=sdk)

    handle = await backend.create()

    assert (handle.sandbox_id, handle.backend) == ("sbx-1", BACKEND_NAME)
    assert ("create", "tmpl-x") in sdk.calls


async def test_exec_joins_argv_and_passes_env_and_timeout() -> None:
    sdk = FakeSdk()
    backend = E2BSandboxBackend(sdk=sdk)
    handle = await backend.create()

    await backend.exec(
        handle,
        ["sh", "-c", "echo $GREETING; sleep 1"],
        env={"GREETING": "hi"},
        timeout_seconds=5.0,
    )

    call = next(c for c in sdk.calls if c[0] == "run")
    assert call[1] == "sh -c 'echo $GREETING; sleep 1'"
    assert call[2] == {"GREETING": "hi"}
    assert call[3] == 5.0


async def test_request_targets_control_port_over_https_with_traffic_token(monkeypatch) -> None:
    sent: dict = {}

    async def fake_request_url(origin, method, path, *, body, headers, timeout_seconds):
        sent.update(
            origin=origin, method=method, path=path, headers=headers,
            body=body, timeout_seconds=timeout_seconds,
        )
        return 200, b"ok"

    monkeypatch.setattr("roost.backends.e2b.request_url", fake_request_url)
    backend = E2BSandboxBackend(control_port=8787, sdk=FakeSdk())
    handle = await backend.create()

    status, body = await backend.request(
        handle, "POST", "/v1/turn", body=b"{}", headers={"x-roost": "1"}, timeout_seconds=3.0
    )

    assert (status, body) == (200, b"ok")
    assert sent["origin"] == "https://8787-sbx-1.e2b.app"
    assert sent["path"] == "/v1/turn"
    assert sent["headers"] == {"x-roost": "1", "e2b-traffic-access-token": "tok"}


async def test_request_omits_traffic_header_when_sandbox_is_public(monkeypatch) -> None:
    sent: dict = {}

    async def fake_request_url(origin, method, path, *, body, headers, timeout_seconds):
        sent.update(headers=headers)
        return 200, b""

    monkeypatch.setattr("roost.backends.e2b.request_url", fake_request_url)
    backend = E2BSandboxBackend(sdk=FakeSdk(FakeSandbox(token=None)))
    handle = await backend.create()

    await backend.request(handle, "GET", "/v1/health")

    assert sent["headers"] == {}


async def test_pause_then_next_use_reconnects_for_implicit_resume() -> None:
    """pause 后必须丢弃缓存的沙箱对象——隐含恢复靠的正是下一次 connect。"""
    sdk = FakeSdk()
    backend = E2BSandboxBackend(sdk=sdk)
    handle = await backend.create()

    await backend.pause(handle)
    await backend.exec(handle, ["echo", "awake"])

    assert [c[0] for c in sdk.calls] == ["create", "pause", "connect", "run"]


async def test_upload_skips_empty_and_batches_files() -> None:
    sdk = FakeSdk()
    backend = E2BSandboxBackend(sdk=sdk)
    handle = await backend.create()

    await backend.upload(handle, {})
    assert not [c for c in sdk.calls if c[0] == "write_files"]

    await backend.upload(handle, {"/opt/a.py": b"x", "/opt/b.py": b"y"})
    call = next(c for c in sdk.calls if c[0] == "write_files")
    assert call[1] == {"/opt/a.py": b"x", "/opt/b.py": b"y"}


def test_recommended_bind_host_is_loopback() -> None:
    """实测结论（见模块 docstring）：E2B 端口代理可达沙箱内 loopback。"""
    assert DEFAULT_BIND_HOST == "127.0.0.1"
