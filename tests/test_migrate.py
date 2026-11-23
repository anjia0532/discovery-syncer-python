"""网关数据迁移（migrate_to）回归测试。

覆盖 v3.0.3 修复的两个缺陷：
1. ssl/proto 资源名旧实现用 ``alias_uris`` 无条件互换，v3 -> v3 时把 ssls 写成
   ``/apisix/admin/ssl``，被 APISIX 3.x 拒绝（404 Unsupported resource type: ssl），
   导致证书全部静默丢失。现在按【目标】网关版本取名。
2. 迁移写入失败原先被 ``apisix_execute`` 归一化成空列表，接口仍返回 OK。
   现在 ``put_raw`` 返回真实状态码，失败汇总后抛错（handler 转 500）。
"""

from __future__ import annotations

import asyncio
import types


class _GWConfig:
    """Gateway 构造所需的配置替身。"""

    def __init__(self, version: str, admin_url: str = "http://apisix:9180"):
        self.admin_url = admin_url
        self.prefix = "/apisix/admin/"
        self.config = {"version": version, "X-API-KEY": "test-key"}
        self.declared_version = version


def _make_gateway(apisix, version: str):
    return apisix.Apisix(_GWConfig(version))


def test_resolve_migrate_uri_by_target_version(apisix_gateway):
    """资源名必须按目标版本决定，而非无条件互换。"""
    r = apisix_gateway.resolve_migrate_uri

    # v3 目标：复数
    assert r("ssls", apisix_gateway.APISIX_V3) == "ssls"
    assert r("ssl", apisix_gateway.APISIX_V3) == "ssls"
    assert r("proto", apisix_gateway.APISIX_V3) == "protos"
    assert r("protos", apisix_gateway.APISIX_V3) == "protos"

    # v2 目标：单数
    assert r("ssls", apisix_gateway.APISIX_V2) == "ssl"
    assert r("ssl", apisix_gateway.APISIX_V2) == "ssl"
    assert r("protos", apisix_gateway.APISIX_V2) == "proto"
    assert r("proto", apisix_gateway.APISIX_V2) == "proto"

    # 其余资源原样返回
    assert r("routes", apisix_gateway.APISIX_V3) == "routes"
    assert r("upstreams", apisix_gateway.APISIX_V2) == "upstreams"


def test_migrate_v3_to_v3_writes_ssls_not_ssl(apisix_gateway):
    """v3 -> v3 必须 PUT 到 ssls/{id}（旧实现会写成 ssl/{id} 并 404）。"""
    origin = _make_gateway(apisix_gateway, apisix_gateway.APISIX_V3)
    target = _make_gateway(apisix_gateway, apisix_gateway.APISIX_V3)

    cert = {"id": "-demo-com-2026-01-01", "snis": ["demo.com"], "cert": "CERT", "key": "KEY",
            "status": 1, "create_time": 1, "update_time": 2}

    def fake_execute(method, uri, params, data=None):
        if uri == "ssls":
            return {"list": [{"key": "/apisix/ssls/-demo-com-2026-01-01", "value": dict(cert)}]}
        return {"list": []}

    calls = []

    def fake_put_raw(uri, payload):
        calls.append((uri, dict(payload)))
        return 200, '{"ok":true}'

    origin.apisix_execute = fake_execute
    target.put_raw = fake_put_raw

    asyncio.run(origin.migrate_to(target))

    assert calls, "应当至少有一次写入"
    uris = [u for u, _ in calls]
    assert uris == ["ssls/-demo-com-2026-01-01"], f"实际写入路径: {uris}"

    _, payload = calls[0]
    # 时间戳与证书有效期由服务端维护，迁移时应剔除
    for dropped in ("create_time", "update_time", "validity_start", "validity_end"):
        assert dropped not in payload


def test_migrate_raises_when_write_fails(apisix_gateway):
    """写入失败必须抛错（而不是静默返回 OK）。"""
    origin = _make_gateway(apisix_gateway, apisix_gateway.APISIX_V3)
    target = _make_gateway(apisix_gateway, apisix_gateway.APISIX_V3)

    def fake_execute(method, uri, params, data=None):
        if uri == "ssls":
            return {"list": [{"value": {"id": "-x-1", "snis": ["x.com"], "cert": "C", "key": "K"}}]}
        return {"list": []}

    def failing_put_raw(uri, payload):
        return 404, '{"error_msg":"Unsupported resource type: ssl"}'

    origin.apisix_execute = fake_execute
    target.put_raw = failing_put_raw

    try:
        asyncio.run(origin.migrate_to(target))
    except RuntimeError as exc:
        msg = str(exc)
        assert "失败" in msg
        assert "HTTP 404" in msg
        assert "ssls/-x-1" in msg
    else:
        raise AssertionError("写入失败时 migrate_to 必须抛错")


def test_migrate_skips_record_without_id(apisix_gateway):
    """缺少 id 的记录跳过并告警，不应拼出 ssls/None。"""
    origin = _make_gateway(apisix_gateway, apisix_gateway.APISIX_V3)
    target = _make_gateway(apisix_gateway, apisix_gateway.APISIX_V3)

    def fake_execute(method, uri, params, data=None):
        if uri == "ssls":
            return {"list": [{"value": {"snis": ["noid.com"]}}]}
        return {"list": []}

    calls = []
    origin.apisix_execute = fake_execute
    target.put_raw = lambda uri, payload: (calls.append(uri) or (200, "{}"))

    asyncio.run(origin.migrate_to(target))
    assert calls == [], f"缺 id 的记录不应写入: {calls}"


def test_migrate_rejects_non_apisix_target(apisix_gateway):
    """目标不是 apisix 时直接断言失败。"""
    origin = _make_gateway(apisix_gateway, apisix_gateway.APISIX_V3)
    try:
        asyncio.run(origin.migrate_to(types.SimpleNamespace()))
    except AssertionError:
        return
    raise AssertionError("非 apisix 目标应触发断言")