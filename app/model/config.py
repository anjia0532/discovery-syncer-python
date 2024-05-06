import re
from enum import Enum
from typing import List, Dict

import httpx
from pydantic import BaseModel, Field, model_validator
from pydantic_yaml import parse_yaml_file_as, parse_yaml_raw_as

from core.lib.logger import for_model

logger = for_model(__name__)


class DiscoveryType(Enum):
    NACOS = "nacos"
    EUREKA = "eureka"


class GatewayType(Enum):
    KONG = "kong"
    APISIX = "apisix"


class Common(BaseModel):
    syncer_api_key: str = Field('', alias="syncer-api-key")

    @model_validator(mode='after')
    def check_common(self) -> 'Common':
        assert self.syncer_api_key and self.syncer_api_key != 'NopU13xRheZng2hqHAwaI0TF5VHNN05G', "安全起见请正确填写接口安全key"
        assert re.compile(r'^(?=.*?[A-Z])(?=.*?[a-z])(?=.*?[0-9])(?=.*?[#?!@$%^&*-]).{32,}$').fullmatch(
            self.syncer_api_key), "请设置复杂安全key（长度最少为32位，必须同时包含字母、数字、特殊字符）"
        return self


class Discovery(BaseModel):
    type: DiscoveryType = None
    weight: float = 1.0
    prefix: str = None
    host: str = None
    config: dict = None

    @model_validator(mode='after')
    def check_discovery(self) -> 'Discovery':
        assert self.type is not None, "type 必填，注册中心类型，值为: nacos, eureka"
        assert 0 < self.weight <= 100, "weight 必填，取值范围为: (0, 100], 默认值为 1.0"
        assert len(self.host) > 0, "host 必填，注册中心地址"
        return self


class Gateway(BaseModel):
    type: GatewayType = None
    admin_url: str = Field(None, alias="admin-url")
    prefix: str = None
    config: dict = None

    @model_validator(mode='after')
    def check_gateways(self) -> 'Gateway':
        assert self.type is not None, "type 必填，网关类型，值为: kong, apisix"
        assert len(self.admin_url) > 0, "admin-url 必填，网关地址"
        return self


class Targets(BaseModel):
    id: str = None
    discovery: str = None
    gateway: str = None
    name: str = None
    enabled: bool = False
    exclude_service: List[str] = Field([], alias="exclude-service")
    upstream_prefix: str = Field(None, alias="upstream-prefix")
    fetch_interval: str = Field("0 0 * * * *", alias="fetch-interval")
    maximum_interval_sec: int = Field(-1, alias="maximum-interval-sec")
    config: dict = {}

    @model_validator(mode='after')
    def check_targets(self) -> 'Targets':
        assert self.discovery is not None, "discovery 必填，注册中心名称"
        assert self.gateway is not None, "gateway 必填，网关名称"
        assert len(self.fetch_interval) > 0, "fetch-interval 必填，同步间隔，格式为 秒 分 时 日 月 周, 默认为每天零点零分 0 0 * * * *"
        if "\"{{." in self.config.get("template", ""):
            logger.warning(
                self.name + "config.template 中存在 {{. }} 变量，请检查是否正确填写，已自动删除该变量，改用系统自带模板，当前值: " + \
                self.config.get('template'))
            self.config.pop('template', None)
        return self


class Config(BaseModel):
    common: Common = Field(..., alias="common")
    discovery_servers: Dict[str, Discovery] = Field(..., alias="discovery-servers")
    gateway_servers: Dict[str, Gateway] = Field(..., alias="gateway-servers")
    targets: List[Targets] = None

    @model_validator(mode='after')
    def check_config(self) -> 'Config':
        assert len(self.discovery_servers), "discovery-servers 必填，注册中心列表"
        assert len(self.gateway_servers), "gateway-servers 必填，网关列表"
        assert len(self.targets) > 0, "targets 必填，同步作业列表"
        assert self.common is not None, "common 必填，常规配置"
        return self


class Settings(BaseModel):
    _config: Config = None
    config_path: str = None

    def load_config(self):
        assert self.config_path and len(self.config_path) > 0, "config must be set"
        if bool(re.match(r'^https?://', self.config_path)):
            self._config = parse_yaml_raw_as(Config, httpx.get(self.config_path, timeout=10, verify=False).text)
        else:
            self._config = parse_yaml_file_as(Config, self.config_path)
        from app.tasks.task_syncer import reload
        reload.publish(msg={})

    @property
    def config(self):
        if self._config is None:
            self.load_config()
        return self._config


settings = Settings()
gateway_clients = {}
discovery_clients = {}
