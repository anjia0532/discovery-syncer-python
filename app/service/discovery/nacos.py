import json
from typing import List

import httpx
from nb_time import NbTime

from app.model.syncer_model import Service, Instance, Registration
from app.service.discovery.discovery import Discovery
from core.lib.logger import for_service

logger = for_service(__name__)


class Nacos(Discovery):
    def __init__(self, config):
        super().__init__(config)

    def get_all_service(self, config: dict, enabled_only: bool = True) -> List[Service]:
        # openapi /nacos/v1/ns/service/list?pageNo=0&pageSize=100&groupName=&namespaceId=
        # catalog 包含是否启用 /nacos/v1/ns/catalog/services?withInstances=true&pageNo=0&pageSize=10&serviceNameParam=&groupNameParam=&namespaceId=zhongtai
        data = {"pageNo": 0, "pageSize": 1000000, "groupNameParam": "", "namespaceId": "",
                "withInstances": True, "hasIpCount": True} | config
        del data["template"]
        resp = self.nacos_execute(uri="ns/catalog/services", params=data)
        logger.info(
            f"拉取 nacos 服务列表,url: {self._config.host}{self._config.prefix}ns/catalog/services, 请求参数: {data}, 响应结果: {resp}")
        services = []
        for item in resp:
            instances = []
            for k, v in item.get("clusterMap", {}).items():
                for instance in v.get("hosts", []):
                    if enabled_only and not instance.get("enabled", False):
                        continue
                    instances.append(
                        Instance(port=instance.get("port", None),
                                 ip=instance.get("ip", None),
                                 weight=instance.get("weight", self._config.weight),
                                 metadata=instance.get("metadata", None),
                                 enabled=instance.get("enabled", False),
                                 ext={
                                     "serviceName": item.get("serviceName", None),
                                     "groupName": item.get("groupName", None),
                                     "clusterName": k,
                                     "namespaceId": data.get("namespaceId", None),
                                     "ephemeral": item.get("ephemeral", None),
                                 }))
            services.append(Service(name=item.get("serviceName"), instances=instances))
        return services

    def get_service_all_instances(self, service_name: str, ext_data: dict, enabled_only: bool = True) -> tuple[
        List[Instance], int]:
        # openapi /nacos/v1/ns/instance/list?serviceName=&groupName=&namespaceId=
        # catalog /nacos/v1/ns/catalog/instances?&serviceName=&clusterName=DEFAULT&groupName=DEFAULT_GROUP&pageSize=10&pageNo=1&namespaceId=
        # /nacos/v1/ns/catalog/instances?&serviceName=retail-strategy-hub&clusterName=DEFAULT&groupName=DEFAULT_GROUP&pageSize=10&pageNo=1&namespaceId=47b6897b-a8fe-448c-9519-37f517482857
        ext_data = (ext_data or {}) | {"serviceName": service_name, "clusterName": "DEFAULT",
                                       "groupName": "DEFAULT_GROUP", "pageSize": 1000000, "pageNo": 1}
        ext_data.pop("template", None)
        resp = self.nacos_execute(uri="ns/catalog/instances", params=ext_data)
        logger.info(
            f"拉取 nacos {service_name} 服务的实例列表,url: {self._config.host}{self._config.prefix}ns/catalog/instances, 请求参数: {ext_data}, 响应结果: {resp}")
        instances = []
        for instance in resp.get("list", []):
            if enabled_only and not instance.get("enabled", False):
                continue
            instances.append(
                Instance(port=instance.get("port", None),
                         ip=instance.get("ip", None),
                         weight=instance.get("weight", None),
                         metadata=instance.get("metadata", None),
                         enabled=instance.get("enabled", None),
                         ext={
                             "serviceName": instance.get("serviceName", None),
                             "groupName": resp.get("groupName", None),
                             "clusterName": instance.get("clusterName", None),
                             "namespaceId": instance.get("namespaceId", None),
                         }))
        return instances, int(NbTime().timestamp)

    def modify_registration(self, registration: Registration, instances: List[Instance]):
        for instance in instances:
            if not instance.change:
                continue
            data = {"ip": instance.ip,
                    "port": instance.port,
                    "weight": instance.weight,
                    "enabled": instance.enabled,
                    "serviceName": registration.service_name,
                    "metadata": json.dumps(instance.metadata)
                    } | instance.ext | registration.ext_data
            resp = self.nacos_execute("PUT", "ns/instance", data)
            logger.info(
                f"修改 nacos 服务实例信息,url: {self._config.host}{self._config.prefix}ns/instance, 请求参数: {data}, 响应结果: {resp}")

    def nacos_execute(self, method="GET", uri=None, params=None, data=None):
        resp_txt = httpx.request(method, f"{self._config.host}{self._config.prefix}{uri}",
                                 params=params, data=data, timeout=10,
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json"}).text

        logger.info(
            f"请求 nacos 接口, method: {method}, url: {self._config.host}{self._config.prefix}{uri}, 请求参数: {params}, 请求数据: {data}, 响应结果: {resp_txt}")
        try:
            resp = json.loads(resp_txt)
        except Exception as e:
            resp = resp_txt
        return resp
