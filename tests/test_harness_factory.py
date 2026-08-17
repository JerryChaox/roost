"""`ROOST_HARNESS` 工厂：driver 启动时选哪个 harness（附录 K）。

护两件事：**默认仍是 EchoHarness**（既有全部行为不因为 M3c 改变），以及
**选不出来时 driver 启动失败**——一个接了 turn 却没有 harness 的 driver 会把
配置错误伪装成一连串模型失败。
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from roost.driver.harness import (
    DEFAULT_HARNESS,
    EchoHarness,
    HarnessLoadError,
    harness_from_env,
    load_harness,
)


def test_default_is_echo_harness() -> None:
    assert DEFAULT_HARNESS == "roost.driver.harness:EchoHarness"
    assert isinstance(harness_from_env({}), EchoHarness)


def test_env_selects_the_factory() -> None:
    harness = harness_from_env({"ROOST_HARNESS": "collections:OrderedDict"})
    assert type(harness).__name__ == "OrderedDict"


@pytest.mark.parametrize(
    "spec",
    [
        "roost.driver.harness",             # 少了 :attr
        ":EchoHarness",                     # 少了 module
        "roost.no_such_module:Thing",       # 导不进来
        "roost.driver.harness:NoSuchName",  # 名字不存在
        "json:dumps",                       # 造不出来（缺必填参数）
    ],
)
def test_bad_specs_raise_harness_load_error(spec: str) -> None:
    with pytest.raises(HarnessLoadError):
        load_harness(spec)


def test_driver_process_exits_when_harness_cannot_load() -> None:
    """端到端：坏 spec 下 `python -m roost.driver` 不该起来（宿主按 boot 失败处理）。"""
    completed = subprocess.run(
        [sys.executable, "-m", "roost.driver"],
        env={**os.environ, "ROOST_HARNESS": "roost.nope:Nope", "ROOST_DRIVER_PORT": "0"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 3
    assert "harness" in completed.stderr
