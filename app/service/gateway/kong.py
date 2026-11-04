"""Kong 网关客户端，兼容 2.x / 3.x。

关键版本差异（Kong 3.0 breaking change）：
* 2.x: ``POST /upstreams/{name}/targets`` 可更新已有 target。
* 3.x: ``POST`` **只能新建**，重复 target 返回 409；更新必须
  ``PUT /upstreams/{name}/targets/{target}``。

旧实现是「先 POST，遇 409 再 PUT」，在 3.x 下每次更新都多一次无效往返。
这里按版本能力矩阵直接选择正确方法。
"""

from string import Template
from typing import List, Tuple

import httpx

from app.model.syncer_model import Instance
from app.service.gateway.gateway import Gateway
from core.lib.logger import for_service

logger = for_service(__name__)

default_kong_upstream_template = """
{
    "name": "$name",
    "tags": ["discovery-syncer-python-auto"]
}
"""

default_kong_target_template = """
{
    "target": "$ip:$port",
    "weight": $weight,
    "tags": ["discovery-syncer-python-auto"]
}
"""


class Kong(Gateway):
    def __init__(self, config):
        super().__init__(config)
        self.service_name_map = {}
        self._capability = None
        self._version_evidence = ""
        self.VERSION = 3

    # -- 版本能力 -----------------------------------------------------------
    def _cap(self):
        """通过 Admin API 根路径的 version 字段探测大版本；显式配置优先。"""
        if self._capability is None:
            from app.compat import Product, detect

            cap, evidence = detect(
                Product.KONG,
                declared=getattr(self._config, "declared_version", "") or None,
                admin_url=self._config.admin_url,
            )
            self._capability = cap
            self._version_evidence = evidence
            self.VERSION = cap.major
            logger.info("Kong 版本判定: %s (%s), target 写入方法: %s",
                        cap.label, evidence, cap.target_write)
        return self._capability

    @property
    def capability(self):
        return self._cap()

    @property
    def version_evidence(self) -> str:
        return self._version_evidence

    @property
    def target_write(self) -> str:
        """当前版本更新 target 使用的 HTTP 方法（2.x=POST, 3.x=PUT）。"""
        return self._cap().target_write

    # -- 查询 ---------------------------------------------------------------
    def get_service_all_instances(self, target: dict, upstream_name: str = None) -> List[Instance]:
        # https://docs.konghq.com/gateway/api/admin-oss/latest/
        upstream_name = self.get_upstream_name(target, upstream_name)
        uri = f"upstreams/{upstream_name}{self._config.config.get('targets_uri', '/targets')}"
        resp = self.kong_execute("GET", uri, {})
        if resp.status_code == 404:
            return []
        self.service_name_map[upstream_name] = True
        instances: List[Instance] = []
        for item in resp.json().get("data", []):
            ip, _, port = str(item.get("target", "")).rpartition(":")
            if not ip or not port:
                continue
            instances.append(Instance(ip=ip, port=int(port), weight=item.get("weight", self._config.weight)))
        return instances

    # -- 写入 ---------------------------------------------------------------
    def sync_instances(self, target: dict, upstream_name: str, diff_ins: list, instances: list):
        upstream_name = self.get_upstream_name(target, upstream_name)
        if not diff_ins:
            logger.info(f"kong {upstream_name} 没有变更实例，跳过同步")
            return
        if upstream_name not in self.service_name_map:
            tpl = Template(target.get("config").get("template", default_kong_upstream_template))
            self.kong_execute("PUT", "upstreams", {}, tpl.substitute(name=upstream_name))
            self.service_name_map[upstream_name] = True

        tpl = Template(default_kong_target_template)
        write_method = self.target_write
        for instance in diff_ins:
            data = None
            if instance.enabled:
                data = tpl.substitute(ip=instance.ip, port=instance.port, weight=instance.weight)
                if write_method == "PUT":
                    # 3.x: 直接 upsert 到具体 target 路径，避免 POST 409 的额外往返。
                    target_uri = f"upstreams/{upstream_name}/targets/{instance.ip}:{instance.port}"
                    self.kong_execute("PUT", target_uri, {}, data)
                else:
                    # 2.x: POST 到集合即可创建/更新。
                    target_uri = f"upstreams/{upstream_name}/targets/"
                    resp = self.kong_execute("POST", target_uri, {}, data)
                    if resp.status_code == 409:
                        # 少数 2.8 补丁版已具备 PUT 语义，兜底兼容。
                        self.kong_execute("PUT", f"upstreams/{upstream_name}/targets/{instance.ip}:{instance.port}",
                                          {}, data)
            else:
                target_uri = f"upstreams/{upstream_name}/targets/{instance.ip}:{instance.port}"
                self.kong_execute("DELETE", target_uri, {}, None)

    def fetch_admin_api_to_file(self, file_name: str) -> Tuple[str, str]:
        # https://docs.konghq.com/gateway/3.6.x/production/deployment-topologies/db-less-and-declarative-config/
        raise Exception("Unrealized")

    def migrate_to(self, target_gateway: 'Gateway'):
        raise Exception("Unrealized")

    def restore_gateway(self, body: str) -> str:
        raise Exception("Unrealized")

    def kong_execute(self, method, uri, params, data=None):
        resp = httpx.request(method, f"{self._config.admin_url}{self._config.prefix}{uri}",
                             params=params, data=data, timeout=10,
                             headers={"Content-Type": "application/json", "Accept": "application/json"})

        logger.info(
            f"请求 kong 接口, method: {method}, url: {self._config.admin_url}{self._config.prefix}{uri}, 请求参数: {params}, 请求数据: {data}, 响应结果: {resp.text}")
        return resp
