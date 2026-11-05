"""Nacos 注册中心客户端，兼容 1.x / 2.x / 3.x。

版本差异（来自 app.compat 能力矩阵）：
* 1.x: ``/nacos/v1/`` OpenAPI，``ns/catalog/*``，UDP 推送订阅（``udpPort``）。
* 2.x: 服务端兼容 v1 HTTP，官方订阅走 gRPC(9848)；这里仍尝试 UDP 兼容层，
  不通则由订阅层自动降级为快照 diff 增量轮询。
* 3.x: **不再兼容 v1/v2 OpenAPI**，改用 ``/nacos/v3/admin/*``（范围查询）与
  ``/nacos/v3/client/*``（单服务）；默认命名空间 ``public``；gRPC 为唯一订阅传输。

所有版本分支都从 ``app.compat`` 的能力矩阵取值，避免版本判断散落各处。
"""

from __future__ import annotations

import json
import time
from typing import Any, List, Tuple

import httpx

from app.model.syncer_model import Instance, Registration, Service
from app.service.discovery.discovery import Discovery
from core.lib.logger import for_service

logger = for_service(__name__)


class Nacos(Discovery):
    """Nacos 客户端。"""

    def __init__(self, config):
        super().__init__(config)
        # 复用连接池：旧实现每个请求 httpx.request() 都新建 TCP 连接，
        # 订阅模式下事件驱动会显著放大请求量（每次 diff 都要拉服务+实例），
        # 高 QPS 时会快速耗尽本地端口。keep-alive 让同 host 的连接可复用。
        self._http = httpx.Client(
            timeout=10,
            verify=False,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )

    # -- 版本能力 -----------------------------------------------------------
    def _cap(self):
        """惰性探测版本能力并缓存（失败回落 1.x，保证旧配置永不因探测失败而崩）。"""
        if self._capability is None:
            try:
                from app.compat import Product, detect

                declared = getattr(self._config, "declared_version", "") or None
                cap, evidence = detect(Product.NACOS, declared=declared, host=self._config.host)
                self._capability = cap
                self._version_evidence = evidence
                logger.info("Nacos 版本判定: %s (%s)", cap.label, evidence)
            except Exception as exc:  # noqa: BLE001
                from app.compat import Product, capability

                self._capability = capability(Product.NACOS, 1)
                self._version_evidence = f"版本探测失败，回落 v1: {exc}"
                logger.warning("Nacos 版本探测失败，回落 v1: %s", exc)
        return self._capability

    def detect_version(self):
        return self._cap()

    @property
    def supports_push(self) -> bool:
        """1.x/2.x/3.x 均有服务端推送能力（3.x 需 gRPC，HTTP 侧仅能查询）。"""
        return True

    @property
    def api_prefix(self) -> str:
        """当前版本实际使用的 HTTP API 前缀。"""
        cap = self._cap()
        if cap.major >= 3:
            return cap.api_prefix
        return self._config.prefix or cap.api_prefix

    @property
    def default_namespace(self) -> str:
        """默认命名空间：1.x/2.x 为空串，3.x 为 ``public``。"""
        return self._cap().default_namespace

    def _url(self, uri: str) -> str:
        """按版本拼出完整 URL。"""
        host = (self._config.host or "").rstrip("/")
        return f"{host}{self.api_prefix}{uri.lstrip('/')}"

    # -- 订阅 ---------------------------------------------------------------
    def subscribe_instance(self, service_name: str, group: str, namespace: str, udp_port: int) -> None:
        """登记服务变更订阅。

        1.x/2.x: ``GET /nacos/v1/ns/instance/list?...&udpPort=<port>``，
        服务端据此向该 UDP 端口推送 ServiceInfo；该请求需周期性重发以续约。

        3.x: 官方仅支持 gRPC 订阅，HTTP 侧无订阅语义，这里显式留空，
        由订阅层的 gRPC Provider（或降级后的快照轮询）接管。

        Args:
            service_name: 服务名。
            group: groupName。
            namespace: namespaceId。
            udp_port: 本地 UDP 接收端口。
        """
        cap = self._cap()
        if cap.major >= 3:
            return
        params = {
            "serviceName": service_name,
            "groupName": group or "DEFAULT_GROUP",
            "namespaceId": namespace if namespace else self.default_namespace,
            "clusters": "DEFAULT",
            "healthyOnly": "false",
            "udpPort": udp_port,
        }
        try:
            self.nacos_execute("GET", "ns/instance/list", params=params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Nacos 订阅请求失败 service=%s err=%s", service_name, exc)
            raise

    # -- 查询 ---------------------------------------------------------------
    def get_all_service(self, config: dict, enabled_only: bool = True) -> List[Service]:
        """拉取全部服务（含实例）。

        1.x/2.x 走 ``ns/catalog/services``；3.x 走 ``ns/service/list``（admin 范围查询，
        因为 3.x client API 明确不支持「查询全部服务列表」）。
        """
        cap = self._cap()
        cfg = dict(config or {})
        cfg.pop("template", None)
        namespace = cfg.get("namespaceId") or self.default_namespace

        if cap.major >= 3:
            params = {"pageNo": 1, "pageSize": 1000000, "namespaceId": namespace,
                      "groupNameParam": cfg.get("groupName", "")}
            resp = self.nacos_execute("GET", cap.extra["catalog_path"], params=params)
            items = self._items(resp, cap)
            return self._parse_v3_services(items, enabled_only, namespace)

        data = {"pageNo": 0, "pageSize": 1000000, "groupNameParam": "", "namespaceId": namespace,
                "withInstances": True, "hasIpCount": True} | cfg
        data.pop("template", None)
        resp = self.nacos_execute(uri="ns/catalog/services", params=data)
        logger.info(
            "拉取 nacos 服务列表,url: %s, 请求参数: %s, 响应结果: %s",
            self._url("ns/catalog/services"), data, resp)
        services = []
        for item in self._items(resp, cap):
            instances = []
            for k, v in (item.get("clusterMap", {}) or {}).items():
                for instance in v.get("hosts", []) or []:
                    if enabled_only and not instance.get("enabled", False):
                        continue
                    instances.append(self._to_instance(instance, k, item, data.get("namespaceId")))
            services.append(Service(name=item.get("serviceName") or item.get("name"), instances=instances))
        return services

    def get_service_all_instances(self, service_name: str, ext_data: dict, enabled_only: bool = True) -> Tuple[
            List[Instance], int]:
        """拉取单个服务的全部实例。

        1.x/2.x 走 ``ns/catalog/instances``；3.x 走 ``ns/instance/list``。
        """
        cap = self._cap()
        ext = dict(ext_data or {})
        ext.pop("template", None)
        namespace = ext.get("namespaceId") or self.default_namespace
        group = ext.get("groupName") or "DEFAULT_GROUP"

        if cap.major >= 3:
            params = {"serviceName": service_name, "groupName": group, "namespaceId": namespace,
                      "pageSize": 1000000, "pageNo": 1}
            resp = self.nacos_execute("GET", cap.extra["instance_path"], params=params)
            raw = resp.get("list", resp.get("data", [])) if isinstance(resp, dict) else []
            hosts = raw if isinstance(raw, list) else []
            instances = [self._to_instance(h, h.get("clusterName", "DEFAULT"), {}, namespace)
                         for h in hosts if not enabled_only or h.get("enabled", True)]
            return instances, int(time.time())

        ext_data = ext | {"serviceName": service_name, "clusterName": "DEFAULT",
                          "groupName": group, "pageSize": 1000000, "pageNo": 1,
                          "namespaceId": namespace}
        ext_data.pop("template", None)
        resp = self.nacos_execute(uri="ns/catalog/instances", params=ext_data)
        logger.info(
            "拉取 nacos %s 服务的实例列表,url: %s, 请求参数: %s, 响应结果: %s",
            service_name, self._url("ns/catalog/instances"), ext_data, resp)
        instances = []
        for instance in (resp.get("list", []) if isinstance(resp, dict) else []):
            if enabled_only and not instance.get("enabled", False):
                continue
            instances.append(self._to_instance(instance, instance.get("clusterName"), resp, namespace))
        return instances, int(time.time())

    # -- 写 -----------------------------------------------------------------
    def modify_registration(self, registration: Registration, instances: List[Instance]):
        cap = self._cap()
        uri = "ns/instance"
        for instance in instances:
            if not instance.change:
                continue
            data = {"ip": instance.ip,
                    "port": instance.port,
                    "weight": instance.weight,
                    "enabled": instance.enabled,
                    "serviceName": registration.service_name,
                    "metadata": json.dumps(instance.metadata)
                    } | (instance.ext or {}) | (registration.ext_data or {})
            if cap.major >= 3 and not data.get("namespaceId"):
                data["namespaceId"] = self.default_namespace
            resp = self.nacos_execute("PUT", uri, data)
            logger.info("修改 nacos 服务实例信息,url: %s, 请求参数: %s, 响应结果: %s",
                        self._url(uri), data, resp)

    # -- 底层请求 -----------------------------------------------------------
    def nacos_execute(self, method="GET", uri=None, params=None, data=None):
        """执行 Nacos HTTP OpenAPI 请求，URL 前缀按版本自动选择。"""
        url = self._url(uri or "")
        resp_txt = self._http.request(method, url, params=params, data=data).text
        logger.info("请求 nacos 接口, method: %s, url: %s, 请求参数: %s, 请求数据: %s, 响应结果: %s",
                    method, url, params, data, resp_txt)
        try:
            return json.loads(resp_txt)
        except (ValueError, TypeError):
            return resp_txt

    def close(self) -> None:
        """释放连接池（仅在客户端被替换/停机时调用）。"""
        try:
            self._http.close()
        except Exception:  # noqa: BLE001
            pass

    # -- 解析辅助 -----------------------------------------------------------
    @staticmethod
    def _items(resp: Any, cap: Any) -> List[dict]:
        """用能力矩阵的 ``list_shape`` 归一化不同版本的列表响应。"""
        try:
            from app.compat import normalize_response

            items = normalize_response(resp, cap.list_shape)
        except Exception:  # noqa: BLE001
            items = resp.get("list", []) if isinstance(resp, dict) else []
        return [i if isinstance(i, dict) else {"value": i} for i in items]

    @staticmethod
    def _to_instance(raw: dict, cluster: str, service: dict, namespace: str) -> Instance:
        """把 Nacos 实例 JSON 转成通用 Instance。"""
        return Instance(
            port=raw.get("port"), ip=raw.get("ip"),
            weight=raw.get("weight") or 1.0,
            metadata=raw.get("metadata") or {},
            enabled=bool(raw.get("enabled", True)),
            ext={
                "serviceName": raw.get("serviceName") or service.get("serviceName") or service.get("name"),
                "groupName": raw.get("groupName") or service.get("groupName") or "DEFAULT_GROUP",
                "clusterName": cluster or "DEFAULT",
                "namespaceId": raw.get("namespaceId") or namespace,
                "ephemeral": raw.get("ephemeral", service.get("ephemeral")),
            })

    def _parse_v3_services(self, items: List[dict], enabled_only: bool, namespace: str) -> List[Service]:
        """尽力解析 Nacos 3.x admin 服务列表（字段随小版本可能略有差异）。"""
        services = []
        for item in items:
            body = item.get("value", item) if isinstance(item, dict) else {}
            name = body.get("serviceName") or body.get("name")
            if not name:
                continue
            hosts = body.get("hosts") or body.get("instances") or []
            if isinstance(hosts, dict):
                hosts = [h for arr in hosts.values() for h in (arr.get("hosts", []) if isinstance(arr, dict) else [])]
            instances = [self._to_instance(h, h.get("clusterName", "DEFAULT"), body, namespace)
                         for h in hosts if isinstance(h, dict)
                         and (not enabled_only or h.get("enabled", True))]
            services.append(Service(name=name, instances=instances))
        return services
