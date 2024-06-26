import json
import pathlib
import tempfile
from string import Template
from threading import Thread
from typing import List
import re

import httpx
import yaml

from app.model.syncer_model import Instance
from app.service import repr_str
from app.service.gateway.gateway import Gateway
from core.lib.logger import for_service

logger = for_service(__name__)

default_apisix_upstream_template = """
{
    "name": "$name",
    "nodes": $nodes,
    "timeout": {
        "connect": 30,
        "send": 30,
        "read": 30
    },
    "type":"roundrobin",
    "desc": "auto sync by https://github.com/anjia0532/discovery-syncer-python-python"
}
"""

APISIX_V2 = "v2"
APISIX_V3 = "v3"

apisix_config_version_comment = {
    APISIX_V2: """
# apisix 2.x modify conf/config.yaml https://apisix.apache.org/docs/apisix/2.15/stand-alone/
# apisix:
#  enable_admin: false
#  config_center: yaml
""",
    APISIX_V3: """
# apisix 3.x modify conf/config.yaml https://apisix.apache.org/docs/apisix/3.2/deployment-modes/#standalone
# deployment:
#  role: data_plane
#  role_data_plane:
#    config_provider: yaml
"""
}

apisix_config_template = """
# Auto generate by https://github.com/anjia0532/discovery-syncer-python, Don't Modify

$VersionComment

# save as conf/apisix.yaml

# Notice!!! Generate apisix version is >>>  $Version  <<< 

$Value

#END
"""

fetch_all_upstream = "upstreams"

apisix_uri_dict = {
    "ssl": {"version": [APISIX_V2], "field": "ssl", "order": 0},
    "ssls": {"version": [APISIX_V3], "field": "ssls", "order": 0},
    "proto": {"version": [APISIX_V2], "field": "proto", "order": 0},
    "protos": {"version": [APISIX_V3], "field": "protos", "order": 0},
    "secrets": {"version": [APISIX_V3], "field": "secrets", "order": 0},
    "plugins/list": {"version": [APISIX_V2, APISIX_V3], "field": "plugins", "order": 0},
    "global_rules": {"version": [APISIX_V2, APISIX_V3], "field": "global_rules", "order": 0},
    "stream_routes": {"version": [APISIX_V2, APISIX_V3], "field": "stream_routes", "order": 0},
    "plugin_configs": {"version": [APISIX_V2, APISIX_V3], "field": "plugin_configs", "order": 0},
    "plugin_metadata": {"version": [APISIX_V2, APISIX_V3], "field": "plugin_metadata", "order": 0},
    "consumers": {"version": [APISIX_V2, APISIX_V3], "field": "consumers", "order": 1},
    "services": {"version": [APISIX_V2, APISIX_V3], "field": "services", "order": 2},
    "upstreams": {"version": [APISIX_V2, APISIX_V3], "field": "upstreams", "order": 2},
    "consumer_groups": {"version": [APISIX_V3], "field": "consumer_groups", "order": 2},
    "routes": {"version": [APISIX_V2, APISIX_V3], "field": "routes", "order": 3},
}

ignore_uris = ["plugins/list"]

alias_uris = {"ssl": "ssls", "proto": "protos", "ssls": "ssl", "protos": "proto"}

has_upstream = ["/apisix/routes/", "/apisix/stream_routes/", "/apisix/services/", "/apisix/upstreams/"]


class Apisix(Gateway):
    def __init__(self, config):
        super().__init__(config)
        self.service_name_map = {}
        self.VERSION = config.config.get("version", APISIX_V2)

    def get_service_all_instances(self, target: dict, upstream_name: str = None) -> List[Instance]:
        # 如果 target.upstream_prefix 存在，则使用 upstream_prefix-upstream_name，否则直接使用 upstream_name
        upstream_name = self.get_upstream_name(target, upstream_name)
        # 如果upstream_name为空，则获取所有upstream的实例
        uri = self.service_name_map.get(upstream_name, fetch_all_upstream)

        resp = self.apisix_execute("GET", uri, {})
        instances = []
        if "list" not in resp:
            resp["list"] = [resp]
        for upstream in resp.get("list", []):
            self.service_name_map[
                upstream.get("value").get("name")] = f"{fetch_all_upstream}/{upstream.get('value').get('id')}"
            if upstream_name != upstream.get("value").get('name'): continue

            if isinstance(upstream.get("value").get("nodes"), list):
                for node in upstream.get("value").get("nodes"):
                    instances.append(Instance(ip=node.get("host"), port=node.get("port"), weight=node.get("weight")))
            elif isinstance(upstream.get("value").get("nodes"), dict):
                for addr, weight in upstream.get("value").get("nodes").items():
                    host, port = addr.split(":")
                    instances.append(Instance(ip=host, port=port, weight=weight))
            break
        return instances

    def sync_instances(self, target: dict, upstream_name: str, diff_ins: list, instances: list):
        if not diff_ins and not instances:
            logger.info(f"没有变更实例，跳过同步")
            return

        # apisix 不支持变量更新nodes，所以diffIns无用，直接用discoveryInstances即可
        nodes_json = json.dumps([{"host": item.ip, "port": item.port, "weight": item.weight} for item in instances])
        method = "PATCH"
        upstream_name = self.get_upstream_name(target, upstream_name)

        uri = self.service_name_map.get(upstream_name)
        if uri:
            uri = f"{uri}/nodes"
            body = nodes_json
        else:
            method = "PUT"
            uri = fetch_all_upstream + "/" + upstream_name
            tpl = Template(target.get("config").get("template", default_apisix_upstream_template))
            body = tpl.substitute(name=upstream_name, nodes=nodes_json)

        resp = self.apisix_execute(method, uri, {}, body)
        logger.info("更新upstream结果: %s", resp)

    def fetch_admin_api_to_file(self, file_name: str):
        """
        从apisix获取upstream信息，并保存到文件
        @return:
        """
        val = {}
        for uri, item in apisix_uri_dict.items():
            if self.VERSION not in item.get("version"):
                continue
            resp = self.apisix_execute("GET", uri, {}, None)
            val[item.get("field")] = []
            for vv in resp.get("list"):
                item_val = vv.get("value", vv)
                if item_val.get("status", 1) == 0:
                    continue
                item_val.pop("update_time", None)
                val[item.get("field")].append(item_val)

        yaml.SafeDumper.org_represent_str = yaml.SafeDumper.represent_str

        yaml.add_representer(str, repr_str, Dumper=yaml.SafeDumper)

        content = Template(apisix_config_template).substitute(
            Value=yaml.safe_dump(val, sort_keys=True, allow_unicode=True, default_flow_style=False),
            VersionComment=apisix_config_version_comment.get(self.VERSION),
            Version=self.VERSION)
        file_name = file_name or tempfile.gettempdir() + "/apisix.yaml"
        pathlib.Path(file_name).parent.mkdir(parents=True, exist_ok=True)
        with open(file_name, "w") as f:
            f.write(content)
        return content, file_name

    async def restore_gateway(self, body: str):
        versions = re.findall(r">>>  (\w+)  <<<", body or '')
        if versions and self.VERSION != versions[0]:
            logger.warning(
                f"通过配置文件还原apisix配置，apisix version: {self.VERSION}, 配置文件 version: {versions[0]} 不同版本导入可能会失败")
        data = yaml.safe_load(body)
        threads = []
        for uri, item in sorted(apisix_uri_dict.items(), key=lambda x: x[1].get("order")):
            item_data = data.get(item.get('field'), [])
            if ignore_uris.__contains__(uri) or self.VERSION not in item.get("version") or len(item_data) == 0:
                continue
            logger.info(f"开始恢复 {uri} 数据 {item_data}")
            for val in item_data:
                t = Thread(target=self.apisix_execute,
                           args=("PUT", uri + "/" + val.get("id", val.get("name")), {}, json.dumps(val)))
                t.start()
                threads.append(t)
        for t in threads:
            t.join()

    async def migrate_to(self, target_gateway: Gateway):
        assert isinstance(target_gateway, Apisix), "目前仅支持apisix网关之间的迁移"

        for uri, item in apisix_uri_dict.items():
            if ignore_uris.__contains__(uri) or self.VERSION not in item.get("version"):
                continue
            resp = self.apisix_execute("GET", uri, {}, None)
            if not resp.get("list"):
                continue

            alias_uri = alias_uris.get(uri, uri)
            threads = []
            for val in resp.get("list"):
                item_val = val.get("value", val)
                item_id = item_val.get("id")
                item_val.pop("create_time", None)
                item_val.pop("update_time", None)
                if "ssl" in alias_uri:
                    item_val.pop("validity_start", None)
                    item_val.pop("validity_end", None)
                item_val = self.translate(target_gateway.VERSION, item_val)
                t = Thread(target=target_gateway.apisix_execute,
                           args=("PUT", alias_uri + "/" + item_id, {}, json.dumps(item_val)))
                t.start()
                threads.append(t)
            for t in threads:
                t.join()

    def translate(self, target_version: str, data: dict) -> dict:
        if self.VERSION == target_version: return data
        # https://apisix.apache.org/docs/apisix/upgrade-guide-from-2.15.x-to-3.0.0/
        # v2 -> v3
        try:
            if self.VERSION == APISIX_V2 and target_version == APISIX_V3:
                # plugin.disable -> plugin._meta.disable
                plugins = data.get("plugins", {})
                for name, plugin in plugins.items():
                    plugin["_meta"] = {"disable": not plugin.get("enable", True)}
                    plugin.pop("enable", None)
                # route.service_protocol -> route.upstream.scheme
                "upstream" in data and "service_protocol" in data and data["upstream"].update(
                    {"scheme": data.pop("service_protocol", None)})
                return data
            # v3 -> v2
            elif self.VERSION == APISIX_V3 and target_version == APISIX_V2:
                # plugin._meta.disable -> plugin.disable
                plugins = data.get("plugins", {})
                for name, plugin in plugins.items():
                    plugin["enable"] = not plugin.get("_meta", {}).get("disable", True)
                    plugin.pop("_meta", None)
                # route.upstream.scheme -> route.service_protocol (enum grpc or http)
                if "upstream" in data:
                    scheme = data["upstream"].pop("scheme", '')
                    if 'grpc' in scheme:
                        data.update({"service_protocol": 'grpc'})
                return data
        except Exception as e:
            logger.error(e)
            raise e
        return data

    def apisix_execute(self, method, uri, params, data=None):
        http_resp = httpx.request(method, f"{self._config.admin_url}{self._config.prefix}{uri}",
                                  params=params, data=data, timeout=10,
                                  headers={"X-API-KEY": self._config.config.get("X-API-KEY"),
                                           "Content-Type": "application/json", "Accept": "application/json"})
        resp_txt = http_resp.text
        if http_resp.status_code >= 400:
            logger.warning(
                f"请求 apisix 接口,version: {self._config.config.get('version')}, method: {method}, url: {self._config.admin_url}{self._config.prefix}{uri}, 请求参数: {params}, 请求数据: {data}, 响应结果: {resp_txt}")
        else:
            logger.info(
                f"请求 apisix 接口,version: {self._config.config.get('version')}, method: {method}, url: {self._config.admin_url}{self._config.prefix}{uri}, 请求参数: {params}, 请求数据: {data}, 响应结果: {resp_txt}")

        resp = json.loads(resp_txt)

        if "error_msg" in resp:
            resp = {"list": []}

        if "plugins/list" == uri:
            plugins = {"list": [{"name": plugin} for plugin in resp]}
            resp = plugins

        if APISIX_V3 != self._config.config.get('version') and "list" not in resp:
            resp["list"] = resp.get("node", {}).get("nodes", [])
            if "value" in resp.get("node", {}):
                resp["list"].append(resp.get("node", {}))
        return resp
