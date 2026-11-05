"""多版本适配层：版本探测 + 能力矩阵。

设计要点
--------
* 只依赖标准库；``httpx`` 按需延迟导入且允许注入，便于单元测试独立加载。
* 「显式配置优先、自动探测兜底」：旧 ``config.yaml`` 不写版本字段时仍能工作。
* 能力矩阵是订阅策略、API 路径与响应形态的**唯一事实来源**，避免版本判断散落在各客户端里。

已核实的版本事实
----------------
* Nacos 1.x: UDP 推送 + ``/nacos/v1/`` OpenAPI，客户端仍需轮询兜底。
* Nacos 2.x: gRPC 双向流（主端口 +1000 = 9848），UDP 废弃但服务端兼容 1.x 客户端；
  仅临时(ephemeral)实例支持订阅。
* Nacos 3.x: gRPC 为唯一传输；``/nacos/v3/admin/*``（范围查询）与 ``/nacos/v3/client/*``（单服务）；
  **不再兼容 1.x/2.x HTTP OpenAPI**；客户端 API 不支持全量服务列表；默认命名空间 ``public``。
* APISIX 2.x: 响应 ``action/count/node.nodes[]``，Admin 端口 9080，``ssl``/``proto`` 单数。
* APISIX 3.x: 响应 ``list[]/total`` 或 ``key/value``，Admin 端口 9180，``ssls``/``protos`` 复数。
* Kong 3.0+: ``POST /targets`` 只能新建，重复 target 返回 409，更新必须 ``PUT``；2.x 允许 POST 更新。
"""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

__all__ = [
    "Product", "PushTransport", "ListShape", "Capability",
    "parse_major", "capability", "probe_nacos", "probe_apisix", "probe_kong",
    "detect", "normalize_response", "probe_tcp",
]


class Product(str, Enum):
    """支持的产品类型（与 config.yaml 的 ``type`` 对应）。"""

    NACOS = "nacos"
    EUREKA = "eureka"
    APISIX = "apisix"
    KONG = "kong"


class PushTransport(str, Enum):
    """上游变更通知通道。"""

    UDP = "udp"            # Nacos 1.x 原生推送（2.x 兼容层仍可能可用）
    GRPC = "grpc"          # Nacos 2.x/3.x 官方订阅通道（主端口 +1000）
    WEBHOOK = "webhook"    # 入站 HTTP 回调，任意版本可叠加
    POLL = "poll"          # 快照 diff 增量轮询（兜底 / Eureka / gRPC 未启用时）
    NONE = "none"          # 无入站能力，纯下游侧（网关）


class ListShape(str, Enum):
    """列表响应的外层形态。"""

    NODE = "node"          # apisix 2.x: {"action","count","node":{"nodes":[...]}}
    LIST = "list"          # apisix 3.x / nacos v3: {"list":[...],"total":N}
    DATA = "data"          # kong / eureka: {"data":[...]}
    CATALOG = "catalog"    # nacos v1 catalog: {"count":N,"list":[...]}
    RAW = "raw"            # 直接就是列表


@dataclass(frozen=True)
class Capability:
    """某个产品某个大版本的能力描述。

    Attributes:
        product: 产品类型。
        major: 大版本号（1/2/3）。
        label: 可读名称，用于日志与 ``/-/versions``。
        api_prefix: HTTP API 路径前缀（相对 host）。
        push: 该版本首选的上游变更通道。
        list_shape: 列表响应形态，``normalize_response`` 依此归一化。
        target_write: 网关侧更新 target/upstream 使用的 HTTP 方法。
        grpc_offset: gRPC 端口相对主端口的偏移量（Nacos 2.x/3.x 为 1000）。
        default_namespace: 默认命名空间（Nacos 3.x 为 ``public``）。
        supports_service_list: 是否提供「一次性查询全部服务」接口（Nacos 3.x client API 不支持）。
        supports_persistent_subscribe: 是否支持订阅持久化实例（Nacos 2.x 起为 False）。
        requires_separate_admin_port: 范围查询是否需要独立 console/admin 端口（Nacos 3.x）。
        notes: 补充说明。
    """

    product: Product
    major: int
    label: str
    api_prefix: str
    push: PushTransport
    list_shape: ListShape
    target_write: str = "PUT"
    grpc_offset: int = 0
    default_namespace: str = ""
    supports_service_list: bool = True
    supports_persistent_subscribe: bool = True
    requires_separate_admin_port: bool = False
    notes: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.product.value}-{self.major}"


# ---------------------------------------------------------------------------
# 能力矩阵
# ---------------------------------------------------------------------------
_MATRIX: Dict[Tuple[Product, int], Capability] = {
    (Product.NACOS, 1): Capability(
        product=Product.NACOS, major=1, label="nacos-1.x",
        api_prefix="/nacos/v1/", push=PushTransport.UDP, list_shape=ListShape.CATALOG,
        supports_persistent_subscribe=True,
        notes="UDP 推送为辅助通道，需保留轮询兜底；订阅参数 udpPort。",
        extra={"subscribe_path": "ns/instance/list", "catalog_path": "ns/catalog/services",
               "instance_path": "ns/catalog/instances", "state_path": "/nacos/v1/console/server/state"},
    ),
    (Product.NACOS, 2): Capability(
        product=Product.NACOS, major=2, label="nacos-2.x",
        api_prefix="/nacos/v1/", push=PushTransport.GRPC, list_shape=ListShape.CATALOG,
        grpc_offset=1000, supports_persistent_subscribe=False,
        notes="官方订阅走 gRPC(9848)；服务端兼容 v1 HTTP，可尝试 UDP 兼容层，失败自动降级增量轮询。",
        extra={"subscribe_path": "ns/instance/list", "catalog_path": "ns/catalog/services",
               "instance_path": "ns/catalog/instances", "state_path": "/nacos/v1/console/server/state"},
    ),
    (Product.NACOS, 3): Capability(
        product=Product.NACOS, major=3, label="nacos-3.x",
        api_prefix="/nacos/v3/admin/", push=PushTransport.GRPC, list_shape=ListShape.LIST,
        grpc_offset=1000, default_namespace="public",
        supports_service_list=False, supports_persistent_subscribe=False,
        requires_separate_admin_port=True,
        notes="不再兼容 v1/v2 OpenAPI；全量服务列表须走 /v3/admin（或独立 console 端口）；"
              "gRPC 是唯一订阅传输；未启用 gRPC 时降级为快照 diff 增量轮询。",
        extra={"subscribe_path": "ns/instance/list", "catalog_path": "ns/service/list",
               "instance_path": "ns/instance/list", "state_path": "/nacos/v3/console/server/state",
               "client_prefix": "/nacos/v3/client/"},
    ),
    (Product.EUREKA, 1): Capability(
        product=Product.EUREKA, major=1, label="eureka-1.x",
        api_prefix="/eureka/", push=PushTransport.POLL, list_shape=ListShape.DATA,
        notes="Eureka 无服务端 push，统一走快照 diff 增量轮询。",
        extra={"apps_path": "apps"},
    ),
    (Product.APISIX, 2): Capability(
        product=Product.APISIX, major=2, label="apisix-2.x",
        api_prefix="/apisix/admin/", push=PushTransport.NONE, list_shape=ListShape.NODE,
        notes="响应包在 node.nodes[]；admin 默认 9080；ssl/proto 单数。",
        extra={"ssl": "ssl", "proto": "proto", "admin_default_port": 9080},
    ),
    (Product.APISIX, 3): Capability(
        product=Product.APISIX, major=3, label="apisix-3.x",
        api_prefix="/apisix/admin/", push=PushTransport.NONE, list_shape=ListShape.LIST,
        notes="响应 list[]/total；admin 默认 9180；ssls/protos 复数。",
        extra={"ssl": "ssls", "proto": "protos", "admin_default_port": 9180},
    ),
    (Product.KONG, 2): Capability(
        product=Product.KONG, major=2, label="kong-2.x",
        api_prefix="/", push=PushTransport.NONE, list_shape=ListShape.DATA,
        target_write="POST",
        notes="POST /targets 可更新已有实体。",
    ),
    (Product.KONG, 3): Capability(
        product=Product.KONG, major=3, label="kong-3.x",
        api_prefix="/", push=PushTransport.NONE, list_shape=ListShape.DATA,
        target_write="PUT",
        notes="3.0 起 POST /targets 仅能新建，重复返回 409，更新必须 PUT /targets/{target}。",
    ),
}


def parse_major(raw: Any) -> Optional[int]:
    """从 ``"v3"`` / ``"3.7.1"`` / ``"2"`` / ``"NACOS/2.5.5"`` 等文本中解析大版本号。

    Args:
        raw: 版本文本或数字。

    Returns:
        大版本号，解析失败返回 ``None``。
    """
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    text = str(raw).strip()
    if not text:
        return None
    match = re.search(r"(?<!\d)(\d+)(?:\.\d+)*", text)
    if not match:
        return None
    major = int(match.group(1))
    return major if major > 0 else None


def capability(product: Product, major: int) -> Capability:
    """取能力描述，未知大版本回落到该产品最接近的已知版本。

    Args:
        product: 产品类型。
        major: 大版本号。

    Returns:
        :class:`Capability`。
    """
    cap = _MATRIX.get((product, major))
    if cap:
        return cap
    known = sorted(m for (p, m) in _MATRIX if p == product)
    if not known:
        raise ValueError(f"不支持的产品: {product}")
    nearest = min(known, key=lambda m: abs(m - major))
    return _MATRIX[(product, nearest)]


HttpGet = Callable[..., Any]
TcpProbe = Callable[[str, int, float], bool]


def _default_http_get(url: str, headers: Optional[dict] = None, timeout: float = 3.0) -> Any:
    """默认 GET 实现（延迟导入 httpx）。

    Args:
        url: 完整 URL。
        headers: 请求头。
        timeout: 超时秒数。

    Returns:
        httpx.Response 或等价对象。
    """
    import httpx  # noqa: WPS433 - 延迟导入，保持本模块可独立加载

    return httpx.get(url, headers=headers or {}, timeout=timeout, verify=False)


def _default_tcp_probe(host: str, port: int, timeout: float = 1.0) -> bool:
    """探测 TCP 端口是否可连接（用于判断 Nacos gRPC 9848 是否开放）。

    Args:
        host: 主机名。
        port: 端口。
        timeout: 超时秒数。

    Returns:
        可连接返回 ``True``。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# 公开别名：供订阅层探测 gRPC(9848) 端口复用。
probe_tcp = _default_tcp_probe


def _version_from_payload(payload: Any) -> Optional[int]:
    """从探测响应 JSON 中提取 version 字段的大版本号。

    Args:
        payload: 已解析的 dict 或原始文本。

    Returns:
        大版本号或 ``None``。
    """
    if isinstance(payload, dict):
        for key in ("version", "server_version", "Version"):
            if key in payload:
                return parse_major(payload[key])
        data = payload.get("data")
        if isinstance(data, dict):
            return _version_from_payload(data)
        return None
    if isinstance(payload, str):
        return parse_major(payload)
    return None


def probe_nacos(
    host: str,
    *,
    http_get: HttpGet = _default_http_get,
    tcp_probe: TcpProbe = _default_tcp_probe,
    timeout: float = 3.0,
) -> Tuple[int, str]:
    """自动探测 Nacos 大版本。

    顺序：v3 state -> v1 state -> gRPC 端口 9848 -> 默认 1.x。
    3.x 明确不再兼容 v1/v2 OpenAPI，因此必须优先尝试 v3 探测路径。

    Args:
        host: Nacos 根地址，如 ``https://nacos.example.com``。
        http_get: 可注入的 GET 函数。
        tcp_probe: 可注入的 TCP 连通性探测函数。
        timeout: 单次探测超时。

    Returns:
        ``(major, evidence)``，evidence 说明判定依据，便于日志与排障。
    """
    base = (host or "").rstrip("/")
    parsed = urlparse(base)
    hostname = parsed.hostname or ""
    main_port = parsed.port or (443 if parsed.scheme == "https" else 8848)

    # 1) Nacos 3.x：客户端 API 存在。无参调用会返回 400（参数校验）或 403（需鉴权），
    #    而 1.x/2.x 对同一路径返回 404，因此这是最可靠的 3.x 判据。
    #    注意：3.x 的 /nacos/v3/console/server/state 实测为 404，不能用作判据。
    for path in ("/nacos/v3/client/ns/instance/list", "/nacos/v3/admin/ns/service/list"):
        try:
            resp = http_get(f"{base}{path}", {}, timeout)
            status = getattr(resp, "status_code", 0)
            if status in (200, 400, 401, 403):
                return 3, f"v3 API reachable with status {status} -> 3.x"
        except Exception:  # noqa: BLE001 - 探测失败继续下一种
            pass

    # 2) v1/v2 state 端点。实测差异：
    #    1.x -> {"version":"1.4.6","standalone_mode":...}
    #    2.x -> {"defaultMaxSize":...,"auth_system_type":"nacos",...}（没有 version 字段）
    for path in ("/nacos/v1/console/server/state", "/nacos/v2/console/server/state"):
        try:
            resp = http_get(f"{base}{path}", {}, timeout)
            if getattr(resp, "status_code", 0) == 200:
                payload = resp.json() if hasattr(resp, "json") else {}
                major = _version_from_payload(payload)
                if major:
                    return min(major, 2), f"state endpoint {path} version -> {major}"
                if isinstance(payload, dict) and (
                        "auth_system_type" in payload or "defaultMaxSize" in payload):
                    return 2, f"state endpoint {path} has 2.x field set -> 2.x"
        except Exception:  # noqa: BLE001
            pass

    grpc_port = main_port + 1000
    if hostname and tcp_probe(hostname, grpc_port, min(timeout, 1.0)):
        return 2, f"gRPC port {grpc_port} reachable -> >=2.x"

    return 1, "no probe evidence, fallback to v1 (最保守，兼容现行配置)"


def probe_apisix(
    admin_url: str,
    prefix: str,
    api_key: str = "",
    *,
    http_get: HttpGet = _default_http_get,
    timeout: float = 3.0,
) -> Tuple[int, str]:
    """按 Admin API 响应形态判定 APISIX 2.x / 3.x。

    2.x 列表响应含 ``action`` / ``count`` / ``node``；3.x 含 ``list`` / ``total``。

    Args:
        admin_url: Admin API 地址。
        prefix: Admin API 路径前缀。
        api_key: X-API-KEY。
        http_get: 可注入的 GET 函数。
        timeout: 超时秒数。

    Returns:
        ``(major, evidence)``。
    """
    url = f"{(admin_url or '').rstrip('/')}{(prefix or '/apisix/admin/').rstrip('/')}/upstreams"
    try:
        resp = http_get(url, {"X-API-KEY": api_key} if api_key else {}, timeout)
        payload = resp.json() if hasattr(resp, "json") else {}
        if isinstance(payload, dict):
            if "node" in payload or "action" in payload or "count" in payload:
                return 2, "response carries action/count/node -> 2.x"
            if "list" in payload or "total" in payload:
                return 3, "response carries list/total -> 3.x"
    except Exception:  # noqa: BLE001
        pass
    return 3, "probe failed, fallback to 3.x"


def probe_kong(
    admin_url: str,
    *,
    http_get: HttpGet = _default_http_get,
    timeout: float = 3.0,
) -> Tuple[int, str]:
    """通过 Admin API 根路径的 ``version`` 字段判定 Kong 大版本。

    Args:
        admin_url: Admin API 地址。
        http_get: 可注入的 GET 函数。
        timeout: 超时秒数。

    Returns:
        ``(major, evidence)``。
    """
    try:
        resp = http_get(f"{(admin_url or '').rstrip('/')}/", {}, timeout)
        payload = resp.json() if hasattr(resp, "json") else {}
        major = _version_from_payload(payload)
        if major:
            return major, f"root version -> {major}"
    except Exception:  # noqa: BLE001
        pass
    return 3, "probe failed, fallback to 3.x (PUT target)"


def detect(
    product: Product,
    declared: Any = None,
    *,
    host: str = "",
    admin_url: str = "",
    prefix: str = "",
    api_key: str = "",
    http_get: HttpGet = _default_http_get,
    tcp_probe: TcpProbe = _default_tcp_probe,
    timeout: float = 3.0,
) -> Tuple[Capability, str]:
    """统一入口：显式配置优先，未配置则自动探测。

    Args:
        product: 产品类型。
        declared: 配置里显式声明的版本（``1`` / ``"v2"`` / ``"3.7.1"`` 等）。
        host: 注册中心地址（Nacos）。
        admin_url: 网关 Admin API 地址。
        prefix: API 路径前缀。
        api_key: 网关 API key。
        http_get: 可注入 GET。
        tcp_probe: 可注入 TCP 探测。
        timeout: 超时。

    Returns:
        ``(capability, evidence)``。
    """
    declared_major = parse_major(declared)
    if declared_major is not None:
        return capability(product, declared_major), f"explicit config version={declared!r}"

    kwargs = dict(http_get=http_get, tcp_probe=tcp_probe, timeout=timeout)
    if product == Product.NACOS:
        major, evidence = probe_nacos(host, **kwargs)
    elif product == Product.APISIX:
        major, evidence = probe_apisix(admin_url, prefix, api_key, http_get=http_get, timeout=timeout)
    elif product == Product.KONG:
        major, evidence = probe_kong(admin_url, http_get=http_get, timeout=timeout)
    else:
        major, evidence = 1, "eureka has single API generation"
    return capability(product, major), evidence


def normalize_response(payload: Any, shape: ListShape) -> list:
    """把不同版本的列表响应统一成 ``list[dict]``。

    Args:
        payload: 已解析的响应体。
        shape: 该版本的列表形态。

    Returns:
        归一化后的条目列表；解析失败返回空列表。
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    if shape == ListShape.NODE:
        node = payload.get("node") or {}
        items = node.get("nodes") or []
        if not items and "value" in node:
            items = [node]
        return list(items)
    if shape in (ListShape.LIST, ListShape.CATALOG):
        items = payload.get("list") or payload.get("data") or []
        if isinstance(items, list) and items:
            return items
        # APISIX 3.x 的单资源响应是 {key, value}，不带 list/total；
        # 统一在这里兜底，避免每个调用点各写一遍特判。
        if "value" in payload:
            return [payload]
        return []
    if shape == ListShape.DATA:
        items = payload.get("data") or []
        return list(items) if isinstance(items, list) else []
    return []
