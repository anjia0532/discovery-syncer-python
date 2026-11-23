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
    "desc": "auto sync by https://github.com/anjia0532/discovery-syncer-python"
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

# ssl/proto 这两组资源在 APISIX 2.x 是单数（/apisix/admin/ssl、/proto），
# 3.x 是复数（/apisix/admin/ssls、/protos）。迁移时目标资源名必须按【目标】网关版本决定，
# 不能像旧版那样无条件互换：v3 -> v3 会把 ssls 写成 /ssl，被 APISIX 拒绝
# （404 Unsupported resource type: ssl），且错误被 apisix_execute 吞成空列表，
# 表现为「迁移成功但证书全丢」。
_ssl_uris = {"ssl", "ssls"}
_proto_uris = {"proto", "protos"}


def resolve_migrate_uri(uri: str, target_version: str) -> str:
    """按【目标】网关版本返回写入用的资源名，其余资源原样返回。"""
    if uri in _ssl_uris:
        return "ssls" if target_version == APISIX_V3 else "ssl"
    if uri in _proto_uris:
        return "protos" if target_version == APISIX_V3 else "proto"
    return uri

has_upstream = ["/apisix/routes/", "/apisix/stream_routes/", "/apisix/services/", "/apisix/upstreams/"]


class Apisix(Gateway):
    def __init__(self, config):
        super().__init__(config)
        self.service_name_map = {}
        self._capability = None
        self._version_evidence = ""
        declared = str((config.config or {}).get("version") or APISIX_V2).lower()
        self.VERSION = APISIX_V3 if declared in ("3", "v3", "3.x") else APISIX_V2
        # 复用连接池，避免事件驱动同步时每请求都新建 TCP 连接。
        self._http = httpx.Client(
            timeout=10,
            verify=False,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            headers={"X-API-KEY": config.config.get("X-API-KEY"),
                     "Content-Type": "application/json", "Accept": "application/json"},
        )

    def close(self) -> None:
        """释放连接池。"""
        try:
            self._http.close()
        except Exception:  # noqa: BLE001
            pass

    # -- 版本能力 -----------------------------------------------------------
    def _cap(self):
        """惰性探测 APISIX 2.x / 3.x；显式 ``version`` 优先，避免额外 HTTP 往返。"""
        if self._capability is None:
            from app.compat import Product, detect

            cap, evidence = detect(
                Product.APISIX,
                declared=getattr(self._config, "declared_version", "") or None,
                admin_url=self._config.admin_url,
                prefix=self._config.prefix or "/apisix/admin/",
                api_key=(self._config.config or {}).get("X-API-KEY", ""),
            )
            self._capability = cap
            self._version_evidence = evidence
            self.VERSION = APISIX_V3 if cap.major >= 3 else APISIX_V2
            logger.info("APISIX 版本判定: %s (%s)", cap.label, evidence)
        return self._capability

    def detect_version(self):
        """探测并返回版本能力（供 /-/versions 与订阅装配统一调用）。"""
        return self._cap()

    @property
    def capability(self):
        return self._capability

    @property
    def version_evidence(self) -> str:
        return self._version_evidence

    def get_service_all_instances(self, target: dict, upstream_name: str = None) -> List[Instance]:
        # 如果 target.upstream_prefix 存在，则使用 upstream_prefix-upstream_name，否则直接使用 upstream_name
        upstream_name = self.get_upstream_name(target, upstream_name)
        # 订阅模式下只同步单个服务，优先按 id 单点读取，避免像旧实现那样拉全量 upstreams。
        uri = self.service_name_map.get(upstream_name) or f"{fetch_all_upstream}/{upstream_name}"

        resp = self.apisix_execute("GET", uri, {})
        items = resp.get("list") or []
        if not items:
            # 兼容 id 与 name 不一致的历史数据：回落全量列表再匹配。
            resp = self.apisix_execute("GET", fetch_all_upstream, {})
            items = resp.get("list") or []
        instances = []
        for upstream in items:
            value = upstream.get("value") or upstream
            name = value.get("name")
            if name:
                self.service_name_map[name] = f"{fetch_all_upstream}/{value.get('id') or name}"
            if upstream_name != name and len(items) > 1:
                continue
            nodes = value.get("nodes")
            if isinstance(nodes, list):
                for node in nodes:
                    instances.append(Instance(ip=node.get("host"), port=node.get("port"), weight=node.get("weight")))
            elif isinstance(nodes, dict):
                for addr, weight in nodes.items():
                    host, _, port = addr.rpartition(":")
                    instances.append(Instance(ip=host, port=int(port), weight=weight))
            if len(items) == 1:
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
        logger.info(f"更新 upstream 结果: {resp}")

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
                item_val.pop("create_time", None)
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

        # 资源名（ssl/ssls、proto/protos）取决于【目标】网关版本，不能用源版本或无条件互换
        target_version = target_gateway.VERSION
        # 按 order 升序：先 ssl/global_rules/plugin_* 等，再 consumers/services/upstreams，
        # 最后 routes，保证路由创建时其引用的 upstream/service 已存在
        ordered = sorted(apisix_uri_dict.items(), key=lambda kv: kv[1].get("order", 0))

        failures: List[str] = []
        migrated = 0

        for uri, item in ordered:
            if ignore_uris.__contains__(uri) or self.VERSION not in item.get("version"):
                continue
            resp = self.apisix_execute("GET", uri, {}, None)
            if not resp.get("list"):
                continue

            target_uri = resolve_migrate_uri(uri, target_version)
            payloads = []
            for val in resp.get("list"):
                item_val = val.get("value", val)
                item_id = item_val.get("id", val.get("id"))
                if item_id is None:
                    logger.warning(f"迁移 {uri} 时跳过缺少 id 的记录: {item_val}")
                    continue
                item_val.pop("create_time", None)
                item_val.pop("update_time", None)
                if target_uri in _ssl_uris:
                    item_val.pop("validity_start", None)
                    item_val.pop("validity_end", None)
                payloads.append((item_id, self.translate(target_version, item_val)))

            if not payloads:
                continue

            results = []

            def _put(_uri=target_uri, _id=None, _val=None):
                status, body = target_gateway.put_raw(f"{_uri}/{_id}", _val)
                results.append((_id, status, body))

            threads = []
            for item_id, item_val in payloads:
                t = Thread(target=_put, kwargs={"_id": item_id, "_val": item_val})
                t.start()
                threads.append(t)
            for t in threads:
                t.join()

            ok_this = 0
            for _id, status, body in results:
                if status >= 400:
                    failures.append(f"{target_uri}/{_id} -> HTTP {status}: {body[:200]}")
                else:
                    ok_this += 1
            migrated += ok_this
            logger.info(f"迁移 {uri} -> {target_uri}: 成功 {ok_this}/{len(payloads)}")

        if failures:
            # 交给 handler 转成 500 + 错误详情，避免「返回 OK 但数据没进去」
            raise RuntimeError(
                f"网关数据迁移存在失败记录，成功 {migrated} 条、失败 {len(failures)} 条: "
                + "; ".join(failures[:10])
            )
        logger.info(f"网关数据迁移完成，共同步 {migrated} 条")

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

    def put_raw(self, uri: str, data: dict):
        """PUT 并返回 (status_code, body)，供迁移等必须感知失败的场景使用。

        与 :meth:`apisix_execute` 的区别：这里**不**把 ``error_msg`` 归一化成空列表，
        调用方可据状态码判断真实结果（旧实现正因吞掉 404 才导致 SSL 静默丢失）。
        """
        resp = self._http.request(
            "PUT", f"{self._config.admin_url}{self._config.prefix}{uri}",
            params={}, data=json.dumps(data),
        )
        return resp.status_code, resp.text

    def apisix_execute(self, method, uri, params, data=None):
        # 复用连接池：事件驱动同步每次 diff 都会打网关 Admin API，
        # 旧实现每个请求新建连接，会产生大量 TIME_WAIT。
        http_resp = self._http.request(
            method, f"{self._config.admin_url}{self._config.prefix}{uri}",
            params=params, data=data,
        )
        resp_txt = http_resp.text
        if http_resp.status_code >= 400:
            logger.warning(
                f"请求 apisix 接口,version: {self._config.config.get('version')}, method: {method}, url: {self._config.admin_url}{self._config.prefix}{uri}, 请求参数: {params}, 请求数据: {data}, 响应结果: {resp_txt}")
        else:
            logger.info(
                f"请求 apisix 接口,version: {self._config.config.get('version')}, method: {method}, url: {self._config.admin_url}{self._config.prefix}{uri}, 请求参数: {params}, 请求数据: {data}, 响应结果: {resp_txt}")

        resp = json.loads(resp_txt)

        if isinstance(resp, dict) and "error_msg" in resp:
            resp = {"list": []}

        if "plugins/list" == uri:
            return {"list": [{"name": plugin} for plugin in resp]}

        if not isinstance(resp, dict):
            return {"list": resp if isinstance(resp, list) else []}

        # 统一按能力矩阵归一化：2.x 包在 node.nodes[]，3.x 单点响应是 {key,value}。
        if "list" not in resp:
            cap = self._cap()
            if cap.major <= 2:
                from app.compat import ListShape, normalize_response

                resp["list"] = normalize_response(resp, ListShape.NODE)
            elif "value" in resp:
                resp["list"] = [resp]
            else:
                resp["list"] = []
        return resp
