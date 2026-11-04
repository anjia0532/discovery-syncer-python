"""测试夹具：在不安装 funboost / redis / nb_log 的环境里加载待测模块。

做法是把 `app` 构造成一个**轻量假包**（不执行 app/__init__.py，避免拉起 FastAPI），
再用 importlib 按文件加载目标模块，并对少数项目内依赖注入 stub。
这样单元测试只依赖 pydantic / PyYAML / pytest，可离线运行。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]


def ensure_pkg(name: str, path: Path | None = None) -> types.ModuleType:
    """在 sys.modules 里放一个轻量包壳。"""
    if name in sys.modules:
        return sys.modules[name]
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)] if path else []
    pkg.__package__ = name
    sys.modules[name] = pkg
    return pkg


def load_module(name: str, rel_path: str, package: str | None = None) -> types.ModuleType:
    """按文件路径加载模块，并注册到 sys.modules 以支持相对 import。"""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    assert spec and spec.loader, f"无法加载 {rel_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    if package:
        module.__package__ = package
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session", autouse=True)
def stub_environment():
    """构造最小运行环境（session 级，只做一次）。"""
    for name, path in (
        ("app", ROOT / "app"),
        ("app.model", ROOT / "app/model"),
        ("app.service", ROOT / "app/service"),
        ("app.compat", ROOT / "app/compat"),
        ("app.subscription", ROOT / "app/subscription"),
        ("core", ROOT / "core"),
        ("core.lib", ROOT / "core/lib"),
    ):
        ensure_pkg(name, path)

    # pydantic_yaml 的真实语义就是 yaml.safe_load + model_validate，测试里等价实现即可。
    py_module = types.ModuleType("pydantic_yaml")

    def parse_yaml_file_as(model, path, **_kwargs):
        with open(path, "r", encoding="utf-8") as handle:
            return model.model_validate(yaml.safe_load(handle))

    def parse_yaml_raw_as(model, raw, **_kwargs):
        return model.model_validate(yaml.safe_load(raw))

    py_module.parse_yaml_file_as = parse_yaml_file_as
    py_module.parse_yaml_raw_as = parse_yaml_raw_as
    sys.modules["pydantic_yaml"] = py_module

    # core.lib.logger
    logger_module = types.ModuleType("core.lib.logger")
    for fn in ("for_model", "for_service", "for_task", "for_handler", "for_middleware", "get"):
        setattr(logger_module, fn, lambda n=None, *_a, **_kw: logging.getLogger(n or "test"))
    sys.modules["core.lib.logger"] = logger_module

    # sync_core 只用到 Instance，用轻量 pydantic 模型替代，避免拉起 db_libs/funboost。
    syncer_model = types.ModuleType("app.model.syncer_model")

    class Instance(BaseModel):
        port: int
        ip: str
        weight: float = 1.0
        metadata: dict = {}
        enabled: bool = False
        change: bool = False
        ext: dict = {}

    syncer_model.Instance = Instance
    sys.modules["app.model.syncer_model"] = syncer_model

    yield


@pytest.fixture(scope="session")
def compat(stub_environment):
    return load_module("app.compat", "app/compat/__init__.py", package="app.compat")


@pytest.fixture(scope="session")
def sub_core(stub_environment):
    return load_module("app.subscription.core", "app/subscription/core.py",
                       package="app.subscription")


@pytest.fixture(scope="session")
def sub_providers(stub_environment, sub_core):
    return load_module("app.subscription.providers", "app/subscription/providers.py",
                       package="app.subscription")


@pytest.fixture(scope="session")
def sub_manager(stub_environment, sub_core, sub_providers):
    return load_module("app.subscription.manager", "app/subscription/manager.py",
                       package="app.subscription")


@pytest.fixture(scope="session")
def sync_core(stub_environment):
    return load_module("app.service.sync_core", "app/service/sync_core.py",
                       package="app.service")


@pytest.fixture(scope="session")
def cron(stub_environment):
    return load_module("app.subscription.cron", "app/subscription/cron.py",
                       package="app.subscription")


@pytest.fixture(scope="session")
def app_config(stub_environment):
    return load_module("app.model.config", "app/model/config.py", package="app.model")
