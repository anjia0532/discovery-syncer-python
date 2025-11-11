import json
from typing import List

import httpx

from app.model.syncer_model import Instance, Registration, Service
from app.service.discovery.discovery import Discovery
from core.lib.logger import for_service

logger = for_service(__name__)

eureka_status = {
    "UP": True,
    "DOWN": False,
    "OUT_OF_SERVICE": False,
    "UNKNOWN": False,
    True: "UP",
    False: "OUT_OF_SERVICE",
}


class Eureka(Discovery):
    def __init__(self, config):
        super().__init__(config)

    def get_all_service(self, config: dict, enabled_only: bool = True) -> List[Service]:
        # https://github.com/Netflix/eureka/wiki/Eureka-REST-operations
        resp = self.eureka_execute(uri="/")
        apps = resp.get("applications", {}).get("application", [])
        services: List[Service] = []
        for app in apps:
            services.append(
                Service(name=app.get("name"), instances=self.get_instances(app.get("instance", []), enabled_only)))
        return services

    def get_service_all_instances(self, service_name: str, ext_data: dict, enabled_only: bool = True) -> tuple[
        List[Instance], int]:
        resp = self.eureka_execute(uri="/" + service_name)
        app = resp.get("application", {})
        instances: List[Instance] = self.get_instances(app.get("instance", []), enabled_only)
        service_up_timestamp = -1
        if instances:
            service_up_timestamp = instances[0].ext.get("serviceUpTimestamp", -1)
        return instances, service_up_timestamp

    def modify_registration(self, registration: Registration, instances: List[Instance]):
        for instance in instances:
            if not instance.change:
                continue
            # PUT /eureka/v2/apps/appID/instanceID/status?value=OUT_OF_SERVICE
            self.eureka_execute(method="PUT", params={"value": eureka_status.get(instance.enabled, "UP")},
                                uri=f"/{registration.service_name}/{instance.ext.get('instanceId')}/status")

    def get_instances(self, instance_array: List, enabled_only: bool = True) -> List[Instance]:

        instances: List[Instance] = []
        for instance in instance_array:
            if enabled_only and not eureka_status.get(instance.get("status"), False):
                continue
            instances.append(Instance(port=instance.get("port", {}).get("$"), ip=instance.get("ipAddr"),
                                      weight=self._config.weight,
                                      metadata=instance.get("metadata", None),
                                      enabled=eureka_status.get(instance.get("status"), False),
                                      ext={
                                          "instanceId": instance.get("instanceId"),
                                          "serviceUpTimestamp": instance.get("leaseInfo", {}).get("serviceUpTimestamp",
                                                                                                  -1) / 1000,
                                      }))
        return instances

    def eureka_execute(self, method="GET", uri=None, params=None, data=None) -> dict:
        resp = httpx.request(method, f"{self._config.host}{self._config.prefix}apps{uri}",
                             params=params, data=data, timeout=10,
                             headers={"Content-Type": "application/json", "Accept": "application/json"})

        logger.info(
            f"请求 eureka 接口, method: {method}, url: {self._config.host}{self._config.prefix}{uri}, 请求参数: {params}, 请求数据: {data}, 响应结果: {resp.text}")
        if resp.status_code == 404:
            return {}
        resp_txt = resp.text
        try:
            resp_dict = json.loads(resp_txt)
        except Exception as e:
            resp_dict = resp_txt
        return resp_dict
