from string import Template
from typing import Tuple, List

import httpx

from app.model.syncer_model import Instance
from app.service.gateway.gateway import Gateway
from core.lib.logger import for_service

logger = for_service(__name__)

default_kong_upstream_template = """
{
    "name": "$name",
    "tags": ["discovery-syncer-auto"]
}
"""

default_kong_target_template = """
{
    "target": "$ip:$port",
    "weight": $weight,
    "tags": ["discovery-syncer-auto"]
}
"""


class Kong(Gateway):
    def __init__(self, config):
        super().__init__(config)
        self.service_name_map = {}

    def get_service_all_instances(self, target: dict, upstream_name: str = None) -> List[Instance]:
        # https://docs.konghq.com/gateway/api/admin-oss/latest/
        upstream_name = self.get_upstream_name(target, upstream_name)
        uri = f"{upstream_name}{self._config.config.get('targets_uri', '/targets')}"
        resp = self.kong_execute("GET", uri, {})
        if resp.status_code == 404:
            return []
        self.service_name_map[upstream_name] = True
        instances: List[Instance] = []
        for item in resp.json().get("data", []):
            ip, port = item.get("target").split(":")
            instances.append(Instance(ip=ip, port=port, weight=item.get("weight", self._config.weight)))
        return instances

    def sync_instances(self, target: dict, upstream_name: str, diff_ins: list, instances: list):
        upstream_name = self.get_upstream_name(target, upstream_name)
        if not diff_ins:
            logger.info(f"kong {upstream_name} 没有变更实例，跳过同步")
            return
        if upstream_name not in self.service_name_map:
            tpl = Template(target.get("config").get("template", default_kong_upstream_template))
            resp = self.kong_execute("POST", "", {}, tpl.substitute(name=upstream_name))
            self.service_name_map[upstream_name] = True

        tpl = Template(default_kong_target_template)
        for instance in diff_ins:
            target_uri = f"{upstream_name}/targets/"
            method = "POST"
            data = None
            if instance.enabled:
                data = tpl.substitute(ip=instance.ip, port=instance.port, weight=instance.weight)
            else:
                target_uri = f"{target_uri}/{instance.ip}:{instance.port}"
                method = "DELETE"
            self.kong_execute(method, target_uri, {}, data)

    def fetch_admin_api_to_file(self) -> Tuple[str, str]:
        # https://docs.konghq.com/gateway/3.6.x/production/deployment-topologies/db-less-and-declarative-config/
        raise Exception("Unrealized")

    def migrate_to(self, target_gateway: 'Gateway'):
        raise Exception("Unrealized")

    def restore_gateway(self, body: str) -> str:
        raise Exception("Unrealized")

    def kong_execute(self, method, uri, params, data=None):
        resp = httpx.request(method, f"{self._config.admin_url}{self._config.prefix}{uri}",
                             params=params, data=data, timeout=10,
                             headers={"X-API-KEY": self._config.config.get("X-API-KEY"),
                                      "Content-Type": "application/json",
                                      "Accept": "application/json"})

        logger.info(
            f"请求 kong 接口, method: {method}, url: {self._config.admin_url}{self._config.prefix}{uri}, 请求参数: {params}, 请求数据: {data}, 响应结果: {resp.text}")
        return resp
