"""版本能力矩阵与自动探测的单元测试（纯离线，注入 fake HTTP/TCP）。"""


class Resp:
    """最小 httpx.Response 替身。"""

    def __init__(self, payload, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._payload


def test_parse_major(compat):
    assert compat.parse_major("v3") == 3
    assert compat.parse_major("3.7.1") == 3
    assert compat.parse_major("2") == 2
    assert compat.parse_major("NACOS/2.5.5") == 2
    assert compat.parse_major("") is None
    assert compat.parse_major(None) is None
    assert compat.parse_major(3) == 3


def test_capability_matrix(compat):
    P = compat.Product
    n1 = compat.capability(P.NACOS, 1)
    n2 = compat.capability(P.NACOS, 2)
    n3 = compat.capability(P.NACOS, 3)
    assert n1.push == compat.PushTransport.UDP and n1.api_prefix == "/nacos/v1/"
    assert n2.push == compat.PushTransport.GRPC and n2.grpc_offset == 1000
    assert not n2.supports_persistent_subscribe
    assert n3.api_prefix == "/nacos/v3/admin/"
    assert n3.default_namespace == "public"
    assert not n3.supports_service_list
    assert n3.requires_separate_admin_port
    # 未知大版本回落到最接近的已知版本
    assert compat.capability(P.NACOS, 9).major == 3

    a2 = compat.capability(P.APISIX, 2)
    a3 = compat.capability(P.APISIX, 3)
    assert a2.list_shape == compat.ListShape.NODE and a2.extra["admin_default_port"] == 9080
    assert a3.list_shape == compat.ListShape.LIST and a3.extra["admin_default_port"] == 9180
    assert a3.extra["ssl"] == "ssls"

    assert compat.capability(P.KONG, 2).target_write == "POST"
    assert compat.capability(P.KONG, 3).target_write == "PUT"


def test_normalize_response(compat):
    S = compat.ListShape
    v2 = {"action": "get", "count": 1,
          "node": {"key": "/apisix/upstreams", "nodes": [{"key": "/apisix/upstreams/1",
                                                          "value": {"id": "1"}}], "dir": True}}
    assert compat.normalize_response(v2, S.NODE)[0]["value"]["id"] == "1"
    v2_single = {"action": "get", "node": {"key": "/apisix/upstreams/1", "value": {"id": "1"}}}
    assert len(compat.normalize_response(v2_single, S.NODE)) == 1
    assert len(compat.normalize_response({"list": [{"value": {}}], "total": 1}, S.LIST)) == 1
    # APISIX 3.x 单资源响应 {key, value} 也必须能归一化
    single = {"key": "/apisix/upstreams/1", "value": {"id": "1", "nodes": []}}
    assert compat.normalize_response(single, S.LIST) == [single]
    assert len(compat.normalize_response({"data": [{"target": "1.1.1.1:80"}]}, S.DATA)) == 1
    assert compat.normalize_response(None, S.LIST) == []


def test_probe_nacos_v3(compat):
    def getter(url, headers, timeout):
        return Resp({"version": "3.3.0"}, 200) if "v3" in url else Resp({}, 404)

    major, evidence = compat.probe_nacos("http://nacos:8848", http_get=getter,
                                         tcp_probe=lambda h, p, t: False)
    assert major == 3 and "v3" in evidence


def test_probe_nacos_v1_and_v2(compat):
    def getter(url, headers, timeout):
        if "v3" in url:
            return Resp({}, 404)
        return Resp({"version": "2.5.5"}, 200)

    major, _ = compat.probe_nacos("http://nacos:8848", http_get=getter,
                                  tcp_probe=lambda h, p, t: False)
    assert major == 2

    def getter_v1(url, headers, timeout):
        return Resp({"version": "1.4.6"}, 200) if "v1" in url else Resp({}, 404)

    major, _ = compat.probe_nacos("http://nacos:8848", http_get=getter_v1,
                                  tcp_probe=lambda h, p, t: False)
    assert major == 1


def test_probe_nacos_grpc_port_fallback(compat):
    def getter(url, headers, timeout):
        return Resp({}, 404)

    major, evidence = compat.probe_nacos("http://nacos:8848", http_get=getter,
                                         tcp_probe=lambda h, p, t: p == 9848)
    assert major == 2 and "9848" in evidence


def test_probe_apisix_response_shape(compat):
    v2 = {"action": "get", "count": 0, "node": {"nodes": [], "dir": True}}
    v3 = {"list": [], "total": 0}
    assert compat.probe_apisix("http://a:9080", "/apisix/admin/", http_get=lambda *a: Resp(v2))[0] == 2
    assert compat.probe_apisix("http://a:9180", "/apisix/admin/", http_get=lambda *a: Resp(v3))[0] == 3


def test_probe_kong_root_version(compat):
    assert compat.probe_kong("http://k:8001", http_get=lambda *a: Resp({"version": "3.7.1"}))[0] == 3
    assert compat.probe_kong("http://k:8001", http_get=lambda *a: Resp({"version": "2.8.1"}))[0] == 2


def test_detect_explicit_config_wins(compat):
    cap, evidence = compat.detect(compat.Product.NACOS, declared="v3", host="http://nacos:8848",
                                  http_get=lambda *a: Resp({"version": "1.4.6"}),
                                  tcp_probe=lambda h, p, t: False)
    assert cap.major == 3 and "explicit" in evidence
