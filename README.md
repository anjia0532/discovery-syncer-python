# 多端注册中心网关同步工具

支持从nacos(已实现)，eureka(已实现)等注册中心同步到apisix(已实现)和kong(已实现)等网关，
后续将支持自定义插件，支持用户自己用 python 实现支持类似携程阿波罗注册中心，etcd注册中心，consul注册中心等插件，以及spring
gateway等网关插件的高扩展性

## 快速开始

### 通过docker运行

```bash
docker run anjia0532/discovery-syncer-python:v2.5.0
```

特别的，`-c ` 支持配置远端http[s]的地址，比如读取静态资源的，比如读取nacos的
`-c http://xxxxx/nacos/v1/cs/configs?tenant=public&group=DEFAULT_GROUP&dataId=discovery-syncer.yaml` ,便于管理

### 配置文件

[点击查看 config.yaml](https://github.com/anjia0532/discovery-syncer-python/blob/master/config-example.yaml)

### Api接口

**注意:**

请勿将此服务暴露到公网，否则对于引发的一切安全事故概不负责。安全起见，所有接口统一都增加 `SYNCER-API-KEY` header
头校验，需要在配置文件 `common.syncer-api-key` 修改默认值，
长度最低为32位，需要同时包含大小写字母，数字，和特殊字符

| 路径                                               | 返回值        | 用途                                                     |
|--------------------------------------------------|:-----------|:-------------------------------------------------------|
| `GET /`                                          | `OK`       | 服务是否启动                                                 |
| `GET /redoc/`                                    | redocly ui | Redocly 接口文档                                           |
| `GET /docs/`                                     | swagger ui | Swagger 接口文档                                           |
| `GET /-/reload`                                  | `OK`       | 重新加载配置文件，加载成功返回OK，主要是cicd场景或者k8s的configmap reload 场景使用 |
| `GET /health`                                    | JSON       | 判断服务是否健康，可以配合k8s等容器服务的健康检查使用                           |
| `PUT /discovery/{discovery-name}`                | `OK`       | 主动下线上线注册中心的服务,配合CI/CD发版业务用                             |
| `GET /gateway-api-to-file/{gateway-name}`        | text/plain | 读取网关admin api转换成文件用于备份或者db-less模式                      |
| `POST /migrate/{gateway-name}/to/{gateway-name}` | `OK`       | 将网关数据迁移(目前仅支持apisix)                                   |
| `PUT /restore/{gateway-name}`                    | `OK`       | 将 db-less 文件还原到网关(目前仅支持apisix)                         |

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

#### `PUT /discovery/{discovery-name}` 主动下线上线注册中心的服务,配合CI/CD发版业务用

discovery-name 是注册中心的名字，如果不存在，则返回 `Not Found` http status code 是404

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
| DEFAULT_USER_EMAIL | git提交时用户名，不传为discovery-syncer-python@syncer.org |
| DEFAULT_BASE_DIR   | git 库clone路径，默认为 syncer                         |

`job.json`
格式类似 https://github.com/anjia0532/discovery-syncer-python/blob/216e36bb3260640e7ca0c6e823344861507fb1fd/tools/backup.py#L117-L124

如果用 docker 的话

```bash
echo '{}' > ./job.json
docker run -e SYNC_JOB_JSON=job.json \
        -e BRANCH=main \
        -e REMOTE=origin \
        -e GIT_REPO='git@github.com:anjia0532/discovery-syncer-python-demo.git' \
        -e DEFAULT_USER_EMAIL='discovery-syncer-python@syncer.org'  \
        -e DEFAULT_BASE_DIR='syncer'  \
        -v $(pwd)/job.json:/opt/discovery-syncer/job.json  \
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

## 待优化点

1. 已解决 ~~目前的同步任务是串行的，如果待同步的量比较大，或者同步时间窗口设置的特别小的情况下，会导致挤压~~

2. 已解决 ~~不支持自定义同步插件，不利于自行扩展~~

3. 同步机制目前是基于定时轮询，效率比较低，有待优化，比如增加缓存开关，上游注册中心与缓存比对没有差异的情况下，不去拉取/变更下游网关的upstream信息，或者看看注册中心支不支持变动主动通知机制等。

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
