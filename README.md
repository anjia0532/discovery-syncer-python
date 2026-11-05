# 多端注册中心网关同步工具

支持从nacos(已实现)，eureka(已实现)等注册中心同步到apisix(已实现)和kong(已实现)等网关，
后续将支持自定义插件，支持用户自己用 python 实现支持类似携程阿波罗注册中心，etcd注册中心，consul注册中心等插件，以及spring
gateway等网关插件的高扩展性

## 快速开始

### 通过docker运行

```bash
docker run anjia0532/discovery-syncer-python:v3.0.0
```

特别的，`-c ` 支持配置远端http[s]的地址，比如读取静态资源的，比如读取nacos的
`-c http://xxxxx/nacos/v1/cs/configs?tenant=public&group=DEFAULT_GROUP&dataId=discovery-syncer.yaml` ,便于管理

**运行时依赖（已简化）**

自 v3 起已移除 funboost 与 Redis：任务执行改为进程内 `ThreadPoolExecutor`
（`app/runtime/taskbus.py`），定时调度改为本地 APScheduler + MemoryJobStore
（`app/runtime/scheduler.py`）。**不再需要任何外部中间件**，一个容器即可运行。

> **单副本约束**：服务持有内存客户端字典、内存订阅管理器、UDP socket 与 SQLite
> 任务库，且去掉了 Redis 分布式锁，必须以单副本运行（K8s `replicas: 1`）。
> 如需多副本 HA，需先外置状态并引入 leader 选举，属于另一量级的改造。

历史说明：v2.7.0 曾引入 Redis 解决内存增长与任务阻塞问题（v2.6.13 及以前无 Redis）。
现在通过「订阅事件去抖 + 5 分钟全量对账兜底」从根本上降低了队列压力，
消息队列与分布式锁都不再必要。

### 配置文件

[点击查看 config.yaml](https://github.com/anjia0532/discovery-syncer-python/blob/master/config-example.yaml)

运行的时候**不再需要配置 `REDIS_*` 环境变量**，也无需启动 Redis。
安装依赖即可：`pip install -r requirements.txt`（显式依赖 `APScheduler`、`nb_log`）。

### Api接口

**注意:**

请勿将此服务暴露到公网，否则对于引发的一切安全事故概不负责。安全起见，所有接口统一都增加 `SYNCER-API-KEY` header
头校验，需要在配置文件 `common.syncer-api-key` 修改默认值，
长度最低为32位，需要同时包含大小写字母，数字，和特殊字符

| 路径                                               | 返回值        | 用途                                                         |
|--------------------------------------------------|:-----------|:-----------------------------------------------------------|
| `GET /`                                          | `OK`       | 服务是否启动                                                     |
| `GET /redoc/`                                    | redocly ui | Redocly 接口文档                                               |
| `GET /docs/`                                     | swagger ui | Swagger 接口文档                                               |
| `GET /-/reload`                                  | `OK`       | 重新加载配置文件，加载成功返回OK，主要是cicd场景或者k8s的configmap reload 场景使用     |
| `GET /show-memory`                               | text/plain | 显示当前内存使用情况，主要是排查内存泄露等问题用                                   |
| `GET /health`                                    | JSON       | 判断服务是否健康，可以配合k8s等容器服务的健康检查使用                               |
| `PUT /discovery/{discovery-name}?alive_num=1`    | `OK`       | 主动下线上线注册中心的服务,配合CI/CD发版业务用                                 |
| `GET /gateway-api-to-file/{gateway-name}`        | text/plain | 读取网关admin api转换成文件用于备份或者db-less模式(目前仅支持apisix,kong建议用deck) |
| `POST /migrate/{gateway-name}/to/{gateway-name}` | `OK`       | 将网关数据迁移(目前仅支持apisix,kong建议用deck)                           |
| `PUT /restore/{gateway-name}`                    | `OK`       | 将 db-less 文件还原到网关(目前仅支持apisix,kong建议用deck)                 |

#### `GET /show-memory?num=20` 查看内存使用情况

num 是显示的条数，默认20条

#### `GET /-/reload` 重新加载配置文件

加载成功返回OK

主要是 cicd 场景或者 k8s 的 configmap reload 场景使用

#### `GET /health` 判断服务是否健康，可以配合k8s等容器服务的健康检查使用

返回值

```json
{
  // 一共有几个enabled的同步任务(targets)
  "total": 2,
  // 正常在跑的有几个
  "running": 2,
  // 有几个超过 配置文件定义的maximum-interval-sec的检测时间没有运行的，失联的。
  "lost": 0,
  // 都在跑，状态是OK（http状态码是200），有在跑的，有失联的，状态是WARN（http状态码是200），全部失联，状态是DOWN(http状态码500)
  "status": "OK",
  // 哪些成功，哪些失败
  "details": [
    "syncer:a_task,is ok",
    "syncer:b-api,is ok"
  ],
  // 运行时长
  "uptime": "1m6s"
}

```

#### `PUT /discovery/{discovery-name}?alive_num=1` 主动下线上线注册中心的服务,配合CI/CD发版业务用

`discovery-name` 是注册中心的名字，如果不存在，则返回 `Not Found` http status code 是404

`alive_num` 是执行上下线操作后，断言存活实例数量，小于等于0则不判断，不传默认1，不满足则报错，http status code 是500, content
类似 `('最少存活实例数1不满足，总实例数(含之前已下线数量)2，要下线实例数2，剩余在线实例数0',)`

body入参

```json
{
  // 检索哪个服务下的实例
  "serviceName": "",
  // 基于注册中心元数据还是基于实例ip来查找
  "type": "METADATA/IP",
  // 匹配的查询条件，支持正则
  "regexpStr": "",
  // 匹配的元数据key，如果是ip则不用填
  "metadataKey": "",
  // 匹配到的将状态改成上线还是下线
  "status": "UP/DOWN",
  // 其他没匹配的，状态是上线还是下线，ORIGIN保持不变
  "otherStatus": "UP/DOWN/ORIGIN",
  // 扩展参数
  "extData": {
  }
}
```

#### `GET /gateway-api-to-file/{gateway-name}?file=/tmp/apisix.yaml` 读取网关admin api转换成文件用于备份或者db-less模式

gateway-name 是网关的名字，如果不存在，则返回 `Not Found`，http status code是404

如果服务报错，resp body 会返回空字符串，header 中的 `syncer-err-msg` 会返回具体原因，http status code 是500，参数 `file`
是可选的，如果不传，则默认`/tmp/文件名` 比如 `/tmp/apisix.yaml`，并返回文件路径

如果正常，resp body 会返回转换后的文本内容，`syncer-file-location` 会返回syncer服务端的路径(
一般是系统临时目录+文件名，例如`/tmp/apisix.yaml`)， http status code是200

**注意**

精力有限，目前仅实现了 apisix 的 admin api 转 yaml，kong 的未实现，有需要的，欢迎提PR贡献代码或者提issues 来反馈

#### `POST /migrate/{origin_gateway_name}/to/{target_gateway_name}` 相同网关不同实例间数据迁移

origin_gateway_name 是源网关的名字，target_gateway_name 是目标网关的名字，如果不存在，则返回 `Not Found` http status code
是404

**注意**

精力有限，目前仅实现了 apisix 实例间数据迁移 （支持 apisix 2x->3x , apisix 3x->2x，apisix 2x->2x，apisix 3x->3x），kong
的未实现，有需要的，欢迎提PR贡献代码或者提issues 来反馈

#### `PUT /restore/{gateway-name}` 将 db-less 文件还原到网关

gateway-name 是网关的名字，如果不存在，则返回 `Not Found` http status code 是404

body入参（以apisix为例）

```yaml
# Auto generate by https://github.com/anjia0532/discovery-syncer-python, Don't Modify

# apisix 2.x modify conf/config.yaml https://apisix.apache.org/docs/apisix/2.15/stand-alone/
# apisix:
#  enable_admin: false
#  config_center: yaml

# apisix 3.x modify conf/config.yaml https://apisix.apache.org/docs/apisix/3.2/deployment-modes/#standalone
# deployment:
#  role: data_plane
#  role_data_plane:
#    config_provider: yaml

# save as conf/apisix.yaml

routes: [ ]
services: [ ]
upstreams: [ ]
plugins: [ ]

#END
```

**注意**

仅限同版本还原，不支持跨版本还原，如apisix 2.x 还原到 apisix 3.x，apisix 3.x 还原到 apisix 2.x。有需要跨版本的
精力有限，目前仅实现了 apisix 的 yaml 还原 apisix ，kong 的未实现，有需要的，欢迎提PR贡献代码或者提issues 来反馈

## 定时备份和还原网关数据

### 备份

| 环境变量               | 值                                               |
|--------------------|:------------------------------------------------|
| SYNC_JOB_JSON      | 同步作业json文件路径                                    |
| GIT_REPO           | git库地址，如果不需要push到库则不设置即可                        |
| REMOTE             | remote名字，不传为origin                              |
| BRANCH             | git分支名字，不传为main                                 |
| USER_EMAIL         | git提交时邮箱，不传为discovery-syncer-python@syncer.org |
| BASE_DIR           | git 库clone路径，默认为 syncer                         |

`job.json` 格式：

```json
{
  "apisix1": {
    "syncer": "http://127.0.0.1:19797",
    "kubectl_forward": {
      "context": "prod-cluster",
      "namespace": "syncer",
      "service": "syncer",
      "remote_port": 9797,
      "local_port": 19797
    },
    "gateways": ["apisix1", "apisix2"],
    "base_name": "apisix.yaml",
    "syncer_api_key": "xxx",
    "timeout": 10,
    "ssl_verify": false
  }
}
```

**kubectl port-forward 支持**：当 syncer 部署在 K8s 集群内时，配置 `kubectl_forward` 字段后，备份工具会自动执行 `kubectl --context <ctx> -n <ns> port-forward <svc> <local>:<remote>`，备份完成后自动关闭隧道。`local_port` 不填则自动分配空闲端口。

> 注意：使用 kubectl port-forward 需要容器内已配置 kubeconfig（挂载 `~/.kube/config` 或设置 `KUBECONFIG` 环境变量）。

如果用 docker 的话

```bash
echo '{}' > ./job.json
docker run -e SYNC_JOB_JSON=job.json \
        -e BRANCH=main \
        -e REMOTE=origin \
        -e GIT_REPO='git@github.com:anjia0532/discovery-syncer-python-demo.git' \
        -e USER_EMAIL='discovery-syncer-python@syncer.org'  \
        -e BASE_DIR='syncer'  \
        -v $(pwd)/job.json:/opt/discovery-syncer/job.json  \
        -v $HOME/.kube:/root/.kube:ro \
        anjia0532/discovery-syncer-python-backup:v2.4.5
```

如果用命令行的话

```bash
wget https://raw.githubusercontent.com/anjia0532/discovery-syncer-python/master/tools/backup.py
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --no-cache-dir --upgrade GitPython httpx

# 安装 git 和 python3.11
echo '{}' > ./job.json
export SYNC_JOB_JSON=job.json
python3 backup.py
```

也支持命令行参数直接指定配置：

```bash
python3 backup.py --config job.json --git-repo git@github.com:xxx/backup.git --branch main
```

### 还原

```bash
vi restore.py

python3 restore.py
```

## 新增服务发现或者网关插件

### 服务发现

修改 [app.model.config.DiscoveryType](https://github.com/anjia0532/discovery-syncer-python/blob/f184e1b67ba0a5a47d2d895a2c22acfb46b61b5f/app/model/config.py#L14-L16)
新增类型,比如

```python
class DiscoveryType(Enum):
    NACOS = "nacos"
    EUREKA = "eureka"
    # 新增 redis
    REDIS = "redis"
```

创建 `app/service/discovery/redis.py` 文件

```python
from typing import List

from app.model.syncer_model import Service, Instance, Registration
from app.service.discovery.discovery import Discovery
from core.lib.logger import for_service

logger = for_service(__name__)


class Redis(Discovery):

    def __init__(self, config):
        super().__init__(config)

    def get_all_service(self, config: dict, enabled_only: bool = True) -> List[Service]:
        pass

    def get_service_all_instances(self, service_name: str, ext_data: dict, enabled_only: bool = True) -> tuple[
        List[Instance], int]:
        pass

    def modify_registration(self, registration: Registration, instances: List[Instance]):
        pass
```

修改 `config.yaml`

```yaml
discovery-servers:
  redis1:
    type: redis
    weight: 100
    prefix: 2
    host: "redis://localhost:6379"
    config:
      demo: demo
gateway-servers:
  apisix1:
    type: apisix
    admin-url: http://apisix-server:9080
    prefix: /apisix/admin/
    config:
      X-API-KEY: xxxxxxxx-xxxx-xxxxxxxxxxxx
      version: v3
targets:
  - discovery: redis
    gateway: apisix1
    enabled: false
    # .. 忽略其他部分
```

### 网关

修改 [app.model.config.GatewayType](https://github.com/anjia0532/discovery-syncer-python/blob/f184e1b67ba0a5a47d2d895a2c22acfb46b61b5f/app/model/config.py#L19-L21)
新增类型,比如

```python
class GatewayType(Enum):
    KONG = "kong"
    APISIX = "apisix"
    SPRING_GATEWAY = "spring_gateway"
```

创建 `app/service/gateway/spring_gateway.py` 文件

```python
from typing import Tuple, List

from app.model.syncer_model import Instance
from app.service.gateway.gateway import Gateway


class SpringGateway(Gateway):
    def __init__(self, config):
        super().__init__(config)

    def get_service_all_instances(self, target: dict, upstream_name: str = None) -> List[Instance]:
        pass

    def sync_instances(self, target: dict, upstream_name: str, diff_ins: list, instances: list):
        pass

    def fetch_admin_api_to_file(self, file_name: str) -> Tuple[str, str]:
        pass

    async def migrate_to(self, target_gateway: 'Gateway'):
        pass
```

修改 `config.yaml`

```yaml
discovery-servers:
  nacos1:
    type: nacos
    weight: 100
    prefix: /nacos/v1/
    host: "http://nacos-server:8858"
gateway-servers:
  spring_gateway1:
    type: spring_gateway
    admin-url: http://gateway-server:9080
    prefix: /
    config:
      demo: xxxxxxxx-xxxx-xxxxxxxxxxxx
targets:
  - discovery: nacos1
    gateway: spring_gateway1
    enabled: false
    # .. 忽略其他部分
```

## 订阅机制与多版本适配

### 从轮询到订阅

| | 旧版 | 新版 |
|---|---|---|
| 取差异方式 | 定时全量拉注册中心 + 全量拉网关，两边比对 | 上游变更事件 -> 只同步变更的**单个服务** |
| 触发频率 | 每个 `fetch-interval` 必然全量跑一遍 | 只有真正发生变化才触发；同服务连续事件按 `debounce-ms` 合并 |
| 无变更开销 | 每次都要拉网关 upstream | 上游快照 diff 无变化 -> 完全不触碰网关 Admin API |
| 兜底 | 无 | 低频全量对账（默认 `@every 5m`）+ 快照 diff 增量轮询自动降级 |
| 失联判定 | `Jobs.last_time`（只在有 diff 时刷新，易误判） | 合并订阅心跳，`/health` 新增 `subscription` 字段 |

订阅层可插拔，按上游版本能力自动选择：

* **Nacos 1.x** — 原生 UDP 推送：本地起 UDP 接收端，周期性重发带 `udpPort` 的
  `GET /nacos/v1/ns/instance/list` 完成订阅续约与新服务发现。
* **Nacos 2.x** — 官方订阅是 gRPC(9848)。服务端保留 UDP 兼容层，因此优先尝试 UDP；
  若 `grpc_stream` 注入了真实双向流则走 gRPC；否则自动降级为快照 diff 增量轮询。
* **Nacos 3.x** — **不再兼容 v1/v2 HTTP OpenAPI**，改用 `/nacos/v3/admin/*`（范围查询）
  与 `/nacos/v3/client/*`（单服务），默认命名空间由 `""` 变为 `public`，
  且客户端 API 不支持「查询全部服务列表」。gRPC 是唯一订阅传输，
  未注入 gRPC 流时自动降级为增量轮询 + 入站 webhook。
* **Eureka** — 无服务端 push，统一走快照 diff 增量轮询。
* **任意版本** — 可叠加 `POST /-/notify` 入站回调，由 CI/CD 或外部监听脚本主动推送。

> 说明：Nacos 2.x/3.x 的 gRPC 订阅协议是基于 protobuf 的双向流。为避免交付一个
> 无法验证的半成品协议客户端，`NacosGrpcSubscription` 采用「可注入流」设计：
> 接入官方 `nacos-sdk-python` 或自研 gRPC 客户端后把事件迭代器注入即可获得真实 push；
> 未注入时明确标记 `degraded` 并自动启用兜底，不会静默丢事件。

### 版本能力矩阵

| 产品 | 版本 | HTTP API | 推送通道 | 列表响应 | 关键差异 |
|---|---|---|---|---|---|
| Nacos | 1.x | `/nacos/v1/` | UDP | `catalog/list` | `ns/catalog/*`，订阅参数 `udpPort` |
| Nacos | 2.x | `/nacos/v1/`（兼容） | gRPC(主端口+1000) | `catalog/list` | 仅临时实例可订阅；`8848/9848/9849` 需同时放行 |
| Nacos | 3.x | `/nacos/v3/admin/`、`/v3/client/` | gRPC（唯一） | `list/total` | 不兼容 v1/v2；默认命名空间 `public`；client API 不支持全量服务列表 |
| APISIX | 2.x | `/apisix/admin/`（9080） | — | `action/count/node.nodes[]` | `ssl`/`proto` 单数 |
| APISIX | 3.x | `/apisix/admin/`（9180） | — | `list[]/total` 或 `key/value` | `ssls`/`protos` 复数 |
| Kong | 2.x | `/`（8001） | — | `data[]` | `POST /targets` 可更新已有 target |
| Kong | 3.x | `/`（8001） | — | `data[]` | **`POST` 只能新建、重复返回 409，更新必须 `PUT /targets/{target}`** |

版本判定顺序：**显式配置优先**（`subscribe.version` / `discovery-servers.<n>.version` /
`gateway-servers.<n>.version`，旧的 `config.version` 继续兼容）→ 自动探测
（Nacos `v3/v1 console/server/state` + 9848 端口；APISIX 响应形态；Kong 根路径 `version`）。
探测失败一律回落到最保守的已知版本，不会让旧配置启动失败。

### 配置兼容

**旧 `config.yaml` 一行不改即可升级**：所有新增字段都是可选的并带默认值。
`fetch-interval` / `maximum-interval-sec` / `healthcheck` / `config.template` /
`exclude-service` / `upstream-prefix` 语义保留；订阅模式下 `fetch-interval` 转作
健康检查与增量轮询周期，全量对账另由 `subscribe.reconcile-interval` 控制（默认 5 分钟）。

需要完全灰度对比时可设置 `subscribe.enabled: false` 或 `subscribe.mode: off`，
行为与旧版定时全量轮询一致。完整可选项见 `config-example.yaml` 末尾注释块。

### 本地运行时（去 funboost / Redis）

| 职责 | 旧实现 | 新实现 |
|---|---|---|
| 异步任务 | `@boost` × 5 + Redis Stream + 5 个 consumer | `app/runtime.TaskBus`（ThreadPoolExecutor + 重试 + 统计） |
| 定时调度 | `funboost_background_scheduler_redis_store` + Redis JobStore/锁 | `app/runtime.LocalScheduler`（APScheduler + MemoryJobStore） |
| 任务上下文 | `funboost_current_task` / `TaskIdLogger` | `core/lib/task_context.py`（ContextVar + `TaskIdFilter`） |
| 队列增强 | 重试×4 / 死信 / 分布式 qps / 20s 过期 / 600s 超时 | 重试×2 + 5m 全量对账兜底 + httpx 10s 超时 |

去掉的能力与补偿：

* **任务持久化**（进程重启丢消息）：由默认 `@every 5m` 的全量对账兜底补齐。
* **死信可观测性**：`GET /-/subscriptions` 的 `_runtime.taskbus` 暴露
  `submitted/succeeded/failed/retries`；且同步失败时**不刷新心跳**，
  `/health` 可通过 `maximum-interval-sec` 感知故障。
* **`function_timeout` 强杀**：Python 线程不可中断，改由各 HTTP 客户端的 10s timeout 保障。
* **分布式控频 / 多副本互斥**：单副本运行，无需该能力。

`/-/reload` 现在显式向本地任务总线提交 `reload`；`Settings.load_config()` 只负责
解析配置，不再隐式触发重载，消除了原先的重复触发与潜在重入。

### 新增运维接口

| 路径 | 用途 |
|---|---|
| `POST /-/notify?discovery=nacos1` | 入站变更回调，body 支持 `serviceName` 或 `services[]`、`groupName`、`namespaceId` |
| `GET /-/subscriptions` | 各作业订阅策略、版本、Provider 健康、降级状态、事件计数 |
| `GET /-/versions` | 各注册中心/网关的版本探测结果与能力矩阵命中情况 |
| `GET /health` | 增加 `subscription` 字段，失联判定改为订阅心跳 |

所有接口仍由 `SYNCER-API-KEY` 中间件保护；`subscribe.notify-token` 可作为回调额外令牌。

### 多版本镜像验证

`tests/integration/docker-compose.matrix.yml` 提供完整版本矩阵：
Nacos `1.4.6` / `2.5.x` / `3.x`、APISIX `2.15` / `3.x`、Kong `2.8` / `3.7`。

```bash
# 在 WSL / Linux 且可运行 docker 的环境执行
cd tests/integration
./run_matrix.sh up        # 拉起矩阵并等待健康检查
./run_matrix.sh probe     # 打印版本探测结果与 /-/versions 对照
./run_matrix.sh test      # 逐个版本跑注册实例 -> 变更 -> 校验网关 upstream
./run_matrix.sh down      # 清理
```

验证项包括：版本探测与镜像大版本一致、APISIX 2.x `node.nodes[]` vs 3.x `list/total`
的响应归一化、upstream `PUT` + `PATCH /nodes` 实际生效，以及 Kong 3.x 重复 `POST /targets`
返回 409 而 `PUT` 幂等成功（2.x 则 `POST` 可连续更新）。

**无 Docker 环境的等价验证**（本地协议级 mock，覆盖完全相同的检查项）：

```bash
python3 tests/integration/run_local_matrix.py     # 需 pip install httpx
```

> 若运行环境使用 Windows + WSL，请注意 WorkBuddy 沙箱默认把 `wsl.exe` 列入程序黑名单，
> 需在「安全中心 → 命令安全 → 程序黑名单」移除后才能在会话内直接调用；
> 也可直接在自己的 WSL 终端里执行上述脚本，结果等价。
> 本机若未安装 Docker Desktop，同样只能用 `run_local_matrix.py` 做协议级验证。

## 发版流程

### 工作流总览

| 工作流 | 触发方式 | 作用 |
|---|---|---|
| `.github/workflows/ci.yml` | PR / push master | 单测 + 冒烟（快速反馈） |
| `.github/workflows/docker-image.yml` | push master | Docker Hub dev 镜像（`latest`） |
| `.github/workflows/release.yml` | **手动** `workflow_dispatch` | 全量测试 → 打 tag → 写 CHANGELOG → 推 gcr.io + Docker Hub |

### 发版步骤

1. 确保工作区在 `master` 分支且已合并所有待发布改动。
2. 在 GitHub Actions 页面选择 **Release** → **Run workflow**，输入版本号（如 `3.0.0`）。
3. 工作流自动执行：
   - **test 作业**：单测（48 项）+ 应用冒烟（含内存压力）+ Docker 多版本矩阵契约验证。
   - 测试通过后进入 **release 作业**：生成 CHANGELOG → 打 tag `v{version}` → 创建 GitHub Release → 推送镜像到 `gcr.io` 与 Docker Hub。
4. 镜像标签：`{version}`、`latest`、`{sha}` 三个。

> 可选：`skip_matrix` 勾选可跳过 Docker 矩阵（仅单测 + 冒烟），加快发版速度。

### GCP OIDC 配置（gcr.io 推送）

release.yml 通过 OIDC workload identity 免密钥推送到 gcr.io。首次使用需在 GCP 完成一次性配置：

1. 在 GCP 创建 workload identity pool 与 provider（参考 [GCP 官方文档](https://cloud.google.com/iam/docs/workload-identity-federation)）。
2. 将 GitHub 仓库加入 provider 的 attribute mapping：
   ```
   https://token.actions.githubusercontent.com|repo:anjia0532/discovery-syncer-python
   ```
3. 在 GitHub 仓库 **Settings → Secrets and variables → Actions → Variables** 添加：

   | 变量名 | 值 |
   |---|---|
   | `GCP_PROJECT_ID` | gcr.io 对应的 GCP 项目 ID |
   | `GCP_WI_POOL_ID` | workload identity pool ID |
   | `GCP_WI_PROVIDER_ID` | workload identity provider ID |

4. Docker Hub 推送使用已有的 `HUB_NAME` / `HUB_PWD` secrets，无需额外配置。

### CHANGELOG 生成

`scripts/gen_changelog.py` 从上一个 `v*` tag 到 HEAD 的提交生成 Keep a Changelog 格式条目，
按 conventional commit 类型（feat / fix / refactor / perf / test / docs / build / chore）自动分组。
release.yml 在打 tag 前自动调用，也可本地手动运行：

```bash
python3 scripts/gen_changelog.py --version 3.0.0
```

## 待优化点

1. 已解决 ~~目前的同步任务是串行的，如果待同步的量比较大，或者同步时间窗口设置的特别小的情况下，会导致挤压~~

2. 已解决 ~~不支持自定义同步插件，不利于自行扩展~~

3. 已解决 ~~同步机制目前是基于定时轮询，效率比较低，有待优化，比如增加缓存开关，上游注册中心与缓存比对没有差异的情况下，不去拉取/变更下游网关的upstream信息，或者看看注册中心支不支持变动主动通知机制等。~~ 已改为「订阅上游 + 事件驱动单服务同步 + 低频全量对账兜底」，并增加多版本能力矩阵，详见下文《订阅机制与多版本适配》。

## 鸣谢

- [APScheduler -- 本地轻量定时调度框架](https://apscheduler.readthedocs.io/)（v3 起替代 funboost 定时任务）
- [funboost -- python万能分布式函数调度框架](https://github.com/ydf0509/funboost)（历史版本使用，v3 起已移除）
- [apisix -- 云原生Api网关](https://github.com/apache/apisix)
- [kong -- 云原生Api网关](https://github.com/Kong/kong)
- [nacos -- 一个更易于构建云原生应用的动态服务发现、配置管理和服务管理平台](https://github.com/alibaba/nacos)
- [eureka -- 中间层服务器的发现、负载平衡和故障转移](https://github.com/Netflix/eureka)
- [discovery-syncer -- 我之前用golang写的多端注册中心网关同步工具](https://github.com/anjia0532/discovery-syncer)

Copyright and License
---

This module is licensed under the BSD license.

Copyright (C) 2017-, by AnJia <anjia0532@gmail.com>.

All rights reserved.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the
following conditions are met:

* Redistributions of source code must retain the above copyright notice, this list of conditions and the following
  disclaimer.

* Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following
  disclaimer in the documentation and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF
THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
