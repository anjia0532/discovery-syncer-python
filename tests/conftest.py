"""测试夹具：在不安装 funboost / redis / nb_log / db_libs / APScheduler 的环境里加载待测模块。

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
        ("app.runtime", ROOT / "app/runtime"),
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


# ---------------------------------------------------------------------------
# 旧版 config.yaml 的兼容性测试数据
# ---------------------------------------------------------------------------
# 真实 config.yaml 被 .gitignore 排除（含生产 API key / 内部地址），CI 检出后不存在。
# 因此测试**不能**依赖该文件，否则本地通过、CI 必失败。
# 这里内置一份「与旧版结构完全等价」的合成配置：覆盖旧版全部字段
# （fetch-interval / maximum-interval-sec / exclude-service / upstream-prefix /
#  config.template / healthcheck / 显式 version），且不含任何真实凭据。
# 若本地存在真实 config.yaml，则优先用它（验证真实配置也能零改动加载）。
LEGACY_CONFIG_YAML = """
common:
  syncer-api-key: Abcdef1234567890!@#Abcdef1234567890

discovery-servers:
    nacos-pre:
        type: nacos
        weight: 100
        prefix: /nacos/v1/
        host: http://nacos.example.com

gateway-servers:
    kong371:
        type: kong
        admin-url: http://127.0.0.1:8001
        prefix: /
        config:
            version: 3.7.1
    apisix3x:
        type: apisix
        admin-url: http://127.0.0.1:31980
        prefix: /apisix/admin/
        config:
            X-API-KEY: 00000000000000000000000000000000
            version: v3
    apisix2x:
        type: apisix
        admin-url: http://127.0.0.1:29180
        prefix: /apisix/admin/
        config:
            X-API-KEY: 00000000000000000000000000000000
            version: v2

targets:
    -   discovery: nacos-pre
        gateway: apisix3x
        enabled: false
        fetch-interval: "@every 10s"
        maximum-interval-sec: 60
        name: mall
        upstream-prefix: mall
        exclude-service: ['mall-search', 'mall-admin']
        config:
            groupName: DEFAULT_GROUP
            namespaceId: mall
            template: |
                {
                    "id": "$name",
                    "name": "$name",
                    "nodes": $nodes,
                    "type": "roundrobin"
                }
        healthcheck:
              type: http
              uri: /
              timeout-sec: 1
              interval: "@every 10s"
              min-hosts: 0
              method: GET
              healthy:
                http_statuses: [404, 200, 201]
                successes: 1
              unhealthy:
                http_statuses: [400, 401, 404, 500, 502, 503, 504]
                failures: 1
                timeouts: 1
"""


@pytest.fixture(scope="session")
def legacy_config_yaml() -> str:
    """返回一份旧版结构的 config.yaml 文本。

    优先使用仓库根目录的真实 ``config.yaml``（开发者本地场景，验证真实配置兼容），
    不存在时回落到内置等价配置（CI 场景，该文件被 gitignore 排除）。
    """
    real = ROOT / "config.yaml"
    if real.exists():
        return real.read_text(encoding="utf-8")
    return LEGACY_CONFIG_YAML


@pytest.fixture(scope="session")
def task_context(stub_environment):
    return load_module("core.lib.task_context", "core/lib/task_context.py", package="core.lib")


@pytest.fixture(scope="session")
def runtime_taskbus(stub_environment, task_context):
    return load_module("app.runtime.taskbus", "app/runtime/taskbus.py", package="app.runtime")


@pytest.fixture(scope="session")
def runtime_scheduler(stub_environment):
    # APScheduler 在 LocalScheduler.start() 内延迟导入，加载模块本身无需安装。
    return load_module("app.runtime.scheduler", "app/runtime/scheduler.py", package="app.runtime")
