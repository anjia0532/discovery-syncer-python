"""配置兼容回归：旧 config.yaml / config-example.yaml 零改动加载。

这是「兼容当前版本配置」的核心保证：所有新增字段必须可选且带默认值。
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
VALID_KEY = "Abcdef1234567890!@#Abcdef1234567890"


def test_old_config_yaml_loads_without_changes(app_config):
    """真实 config.yaml 不含任何订阅/版本字段，必须能原样解析。"""
    cfg = app_config.parse_yaml_file_as(app_config.Config, str(ROOT / "config.yaml"))
    assert len(cfg.targets) == 1
    target = cfg.targets[0]
    assert target.fetch_interval == "@every 10s"
    assert target.exclude_service  # 旧字段保持
    assert target.config.get("template")  # 旧模板保持
    # 新字段自动取默认值
    assert target.subscribe.enabled is True
    assert target.subscribe.mode == app_config.SubscribeMode.AUTO
    assert target.subscribe.reconcile is True
    assert target.subscribe.webhook is True
    assert target.subscribe.fallback is True
    assert target.subscribe.version == ""
    assert target.subscribe.udp_port == 0


def test_example_yaml_targets_load(app_config):
    """示例配置除默认 key 外必须可解析（示例 key 被校验器有意拒绝，替换后再测）。"""
    raw = (ROOT / "config-example.yaml").read_text(encoding="utf-8")
    raw = raw.replace("NopU13xRheZng2hqHAwaI0TF5VHNN05G", VALID_KEY)
    cfg = app_config.parse_yaml_raw_as(app_config.Config, raw)
    assert len(cfg.targets) == 2
    assert cfg.targets[0].fetch_interval == "*/30 * * * * *"
    assert cfg.targets[1].discovery == "eureka1"
    assert all(t.subscribe.mode == app_config.SubscribeMode.AUTO for t in cfg.targets)


def test_example_yaml_rejects_default_api_key(app_config):
    """保留原有安全约束：示例默认 key 必须被拒绝。"""
    with pytest.raises(Exception):
        app_config.parse_yaml_file_as(app_config.Config, str(ROOT / "config-example.yaml"))


def test_declared_version_compat(app_config):
    cfg = app_config.parse_yaml_file_as(app_config.Config, str(ROOT / "config.yaml"))
    assert cfg.gateway_servers["apisix3x"].declared_version == "v3"
    assert cfg.gateway_servers["apisix2x"].declared_version == "v2"
    assert cfg.gateway_servers["kong371"].declared_version == "3.7.1"
    assert cfg.discovery_servers["nacos-pre"].declared_version == ""   # 未声明 -> 自动探测


def test_subscribe_explicit_override(app_config):
    """显式配置可覆盖默认值，且 alias 写法（kebab-case）生效。"""
    raw = (ROOT / "config.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    data["targets"][0]["subscribe"] = {
        "mode": "off", "udp-port": 19999, "debounce-ms": 100,
        "reconcile-interval": "@every 1m", "notify-token": "secret",
    }
    cfg = app_config.parse_yaml_raw_as(app_config.Config, yaml.safe_dump(data, allow_unicode=True))
    sub = cfg.targets[0].subscribe
    assert sub.mode == app_config.SubscribeMode.OFF
    assert sub.udp_port == 19999 and sub.debounce_ms == 100
    assert sub.reconcile_interval == "@every 1m"
    assert sub.notify_token == "secret"


def test_cron_expressions_still_compatible(cron):
    """time_parser 搬迁后对旧 fetch-interval 写法保持 100% 兼容。"""
    assert cron.time_parser("@every 10s")[:6] == ("*/10", "*", "*", "*", "*", "*")
    assert cron.time_parser("*/30 * * * * *")[:6] == ("*/30", "*", "*", "*", "*", "*")
    assert cron.time_parser("*/30 * * * *")[:6] == ("*", "*/30", "*", "*", "*", "*")
    assert cron.time_parser("@daily")[:6] == ("0", "0", "0", "*", "*", "*")
    assert cron.time_parser("@hourly")[:6] == ("0", "0", "*", "*", "*", "*")
    assert cron.time_parser("@reboot")[6] is not None
    assert cron.cron_kwargs("@every 1m")["trigger_fields"]["minute"] == "*/1"
