import re
from enum import Enum
from typing import Dict, List

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_yaml import parse_yaml_file_as, parse_yaml_raw_as

from core.lib.logger import for_model

logger = for_model(__name__)


class HealthCheckMethod(Enum):
    GET = "GET"
    POST = "POST"


class HealthCheckType(Enum):
    HTTP = "http"
    HTTPS = "https"


class DiscoveryType(Enum):
    NACOS = "nacos"
    EUREKA = "eureka"


class GatewayType(Enum):
    KONG = "kong"
    APISIX = "apisix"


class SubscribeMode(str, Enum):
    """订阅策略。

    与 ``app.subscription.core.SubscribeMode`` 取值一一对应，
    这里独立定义是为了让配置层不反向依赖业务层。
    """

    AUTO = "auto"        # 按上游版本能力自动选择，失败自动降级（默认）
    PUSH = "push"        # 强制原生推送（Nacos 1.x UDP / 2.x+ gRPC）
    WEBHOOK = "webhook"  # 仅入站 HTTP 回调
    POLL = "poll"        # 强制快照 diff 增量轮询
    OFF = "off"          # 关闭订阅，完全回退旧版定时全量轮询（灰度对比用）


class SubscriptionConfig(BaseModel):
    """订阅配置。**全部字段可选且带默认值**，旧 config.yaml 无需改动即可加载。

    Attributes:
        enabled: 是否启用订阅；False 等价于 ``mode: off``。
        mode: 订阅策略。
        version: 显式声明上游大版本（``1``/``2``/``3`` 或完整版本号），留空自动探测。
        udp_host / udp_port: Nacos 1.x UDP 监听地址；port=0 随机端口。
        debounce_ms: 同服务事件合并窗口。
        refresh_interval: UDP 订阅续约 / 新服务发现周期，留空默认 ``@every 5m``
            （v3.0.2 起不再复用 ``fetch-interval``，避免高频拉全量服务列表）。
        reconcile_interval: 兜底全量对账周期，留空默认 ``@every 5m``
            （复用 fetch-interval 等于退回高频轮询，故不复用）。
        push_timeout_sec: 超过该秒数未收到真实推送即判定失联并降级
            （0 表示使用内置默认 120s；-1/极大值可视为不判定）。
        reconcile: 是否保留低频全量对账兜底。
        webhook: 是否附带入站 webhook receiver。
        fallback: 推送不可用时是否自动叠加快照 diff 增量轮询。
        notify_token: 入站 webhook 额外令牌（可选，仍受 SYNCER-API-KEY 保护）。
    """

    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = True
    mode: SubscribeMode = SubscribeMode.AUTO
    version: str = ""
    udp_host: str = Field("0.0.0.0", alias="udp-host")
    udp_port: int = Field(0, alias="udp-port", ge=0, le=65535)
    debounce_ms: int = Field(500, alias="debounce-ms", ge=0)
    refresh_interval: str = Field("", alias="refresh-interval")
    reconcile_interval: str = Field("", alias="reconcile-interval")
    push_timeout_sec: int = Field(0, alias="push-timeout-sec", ge=0)
    reconcile: bool = True
    webhook: bool = True
    fallback: bool = True
    notify_token: str = Field("", alias="notify-token")

    @model_validator(mode='after')
    def check_subscription(self) -> 'SubscriptionConfig':
        assert self.debounce_ms >= 0, "subscribe.debounce-ms 不能为负数"
        assert 0 <= self.udp_port <= 65535, "subscribe.udp-port 取值范围 [0, 65535]"
        return self


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
    # 可选：显式声明上游大版本（nacos 1/2/3）。留空则自动探测，旧配置无需改动。
    version: str = ""

    @model_validator(mode='after')
    def check_discovery(self) -> 'Discovery':
        assert self.type is not None, "type 必填，注册中心类型，值为: nacos, eureka"
        assert 0 < self.weight <= 100, "weight 必填，取值范围为: (0, 100], 默认值为 1.0"
        assert len(self.host) > 0, "host 必填，注册中心地址"
        return self

    @property
    def declared_version(self) -> str:
        """显式版本：顶层 ``version`` 优先，兼容旧的 ``config.version`` 写法。"""
        return self.version or str((self.config or {}).get("version") or "")


class Gateway(BaseModel):
    type: GatewayType = None
    admin_url: str = Field(None, alias="admin-url")
    prefix: str = None
    config: dict = None
    # 可选：显式声明网关大版本（apisix v2/v3、kong 2.x/3.x）。留空自动探测。
    version: str = ""

    @model_validator(mode='after')
    def check_gateways(self) -> 'Gateway':
        assert self.type is not None, "type 必填，网关类型，值为: kong, apisix"
        assert len(self.admin_url) > 0, "admin-url 必填，网关地址"
        return self

    @property
    def declared_version(self) -> str:
        """显式版本：顶层 ``version`` 优先，兼容旧的 ``config.version`` 写法。"""
        return self.version or str((self.config or {}).get("version") or "")


class HealthCheck(BaseModel):
    type: HealthCheckType = HealthCheckType.HTTP
    uri: str = "/"
    timeout_sec: int = Field(5, alias="timeout-sec")
    interval: str = None
    min_hosts: int = Field(1, alias="min-hosts")
    method: HealthCheckMethod = HealthCheckMethod.GET
    healthy: dict = {}
    unhealthy: dict = {}
    alert: dict = {}

    @model_validator(mode='after')
    def health_check(self) -> 'HealthCheck':
        assert self.type is not None, "type 必填，访问方式: http,https"
        assert self.timeout_sec > 0, "timeout-sec 必填，超时时间，单位秒，默认5秒"
        assert len(self.interval) > 0, "interval 必填，检查定时表达式"
        assert self.min_hosts > 0, "min-hosts 必填，最小可用节点数，如果低于等于，不会下线节点"
        assert len(self.healthy.get("http_statuses", [])) > 0, "healthy.http_statuses 必填，健康 http 状态码集合"
        assert self.healthy.get("successes", 0) > 0, "healthy.successes 必填，确定节点健康的次数"
        assert len(self.unhealthy.get("http_statuses", [])) > 0, "healthy.http_statuses 必填，不健康 http 状态码集合"
        assert self.unhealthy.get("failures", 0) > 0, "unhealthy.failures 必填，确定节点非健康的次数"
        return self


class Targets(BaseModel):
    id: str = None
    discovery: str = None
    gateway: str = None
    name: str = None
    enabled: bool = False
    exclude_service: List[str] = Field([], alias="exclude-service")
    upstream_prefix: str = Field(None, alias="upstream-prefix")
    # 订阅化后语义变更为「兜底全量对账周期 / 健康检查周期」，字段名与默认值保持不变。
    fetch_interval: str = Field("0 0 * * * *", alias="fetch-interval")
    maximum_interval_sec: int = Field(-1, alias="maximum-interval-sec")
    config: dict = {}
    healthcheck: dict = None
    # 新增但全可选：旧 config.yaml 不写该块时自动使用默认值（AUTO 订阅 + 兜底对账）。
    subscribe: SubscriptionConfig = Field(default_factory=SubscriptionConfig, alias="subscribe")

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


class ClusterConfig(BaseModel):
    """集群（多副本）配置。**默认关闭**，旧 config.yaml 无需改动。

    单副本部署时保持 ``enabled: false``（默认），行为与 v3.0.0 完全一致、不依赖 Redis。
    多副本部署时置 ``enabled: true`` 并配置 redis-url，各副本通过 Redis 抢租约选举
    leader：**只有 leader 装配订阅与定时调度并写网关**，其余副本待命，
    leader 失联后自动接管，避免多副本重复写网关。

    Attributes:
        enabled: 是否启用集群模式。False 时完全不触碰 Redis。
        redis_url: Redis 连接串，如 ``redis://:pwd@127.0.0.1:6379/0``。
        key_prefix: 键前缀，多套环境共用同一 Redis 时用于隔离。
        lock_ttl_sec: leader 租约 TTL（秒）。副本崩溃后最多这么久才被接管。
        renew_interval_sec: 租约续约周期（秒），应明显小于 lock_ttl_sec。
        instance_id: 本副本标识；留空自动生成 ``<hostname>-<pid>-<rand>``。
        redis_timeout_sec: Redis 连接/读写超时（秒）。
        require_redis: True 时 Redis 不可用即启动失败；
            False（默认）时降级为单机模式（各自为 leader），
            适用于从单副本平滑迁移，但多副本下会重复写网关，需谨慎。
    """

    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = False
    redis_url: str = Field("", alias="redis-url")
    key_prefix: str = Field("discovery-syncer", alias="key-prefix")
    lock_ttl_sec: int = Field(30, alias="lock-ttl-sec", ge=5)
    renew_interval_sec: int = Field(10, alias="renew-interval-sec", ge=1)
    instance_id: str = Field("", alias="instance-id")
    redis_timeout_sec: float = Field(3.0, alias="redis-timeout-sec", gt=0)
    require_redis: bool = Field(False, alias="require-redis")

    @model_validator(mode='after')
    def check_cluster(self) -> 'ClusterConfig':
        if not self.enabled:
            return self
        assert self.redis_url, "cluster.enabled=true 时必须配置 cluster.redis-url"
        assert self.renew_interval_sec < self.lock_ttl_sec, \
            "cluster.renew-interval-sec 必须小于 lock-ttl-sec，否则租约会在续约前过期"
        assert self.lock_ttl_sec >= 3 * self.renew_interval_sec, \
            "建议 lock-ttl-sec >= 3 * renew-interval-sec，避免网络抖动导致误失锁"
        return self

    @property
    def lock_key(self) -> str:
        """leader 租约键。"""
        return f"{self.key_prefix}:leader"


class Config(BaseModel):
    common: Common = Field(..., alias="common")
    discovery_servers: Dict[str, Discovery] = Field(..., alias="discovery-servers")
    gateway_servers: Dict[str, Gateway] = Field(..., alias="gateway-servers")
    targets: List[Targets] = None
    # 可选：集群（多副本）配置。默认关闭，旧配置零改动。
    cluster: ClusterConfig = Field(default_factory=ClusterConfig, alias="cluster")

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
        # reload 由调用方显式触发（startup 同步调用 / /-/reload 提交到本地任务总线）。
        # 这里不再自动发布，避免 reload 内部访问 config 时形成重复触发。

    @property
    def config(self):
        if self._config is None:
            self.load_config()
        return self._config


settings = Settings()
gateway_clients = {}
discovery_clients = {}
