# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.0.1] - 2026-11-06

新增**可选**的多副本集群模式。默认关闭，单副本部署行为与 3.0.0 完全一致，不依赖 Redis。

### Features

* **集群模式（多副本）**：配置 `cluster.enabled: true` + `redis-url` 后，
  各副本通过 Redis 租约选举 leader，只有 leader 装配订阅与定时作业并写网关，
  其余副本待命并自动接管，解决多副本重复写网关的问题。
* 选举实现（`app/runtime/cluster.py`）：
  `SET NX PX` 抢锁；Lua「校验持有者 + PEXPIRE」续约，不会续上别人的锁；
  Lua「校验持有者 + DEL」释放，不会误删新任 leader 的锁。
* leader 失联后由 TTL 自然过期触发接管（默认 30s，可配置）；
  优雅停机主动释放租约，其他副本**立即**接管。
* 新增运维接口 `GET /-/cluster`（角色、租约剩余、接管/失锁计数）；
  `/-/subscriptions` 的 `_runtime.cluster` 同步暴露同样信息。
* `/health` 感知集群角色：follower 只要 Redis 连通即返回 UP，
  **避免 K8s 把待命副本反复重启**。

### Configuration

* `cluster` 配置块全部可选带默认值，旧 `config.yaml` 零改动即可升级：
  `enabled`（默认 false）、`redis-url`、`key-prefix`、`lock-ttl-sec`（30）、
  `renew-interval-sec`（10）、`instance-id`、`redis-timeout-sec`（3）、
  `require-redis`（false）。
* 校验：`enabled=true` 必须有 `redis-url`；`renew-interval-sec < lock-ttl-sec`；
  建议 `lock-ttl-sec >= 3 × renew-interval-sec`。
* `require-redis: false`（默认）时 Redis 故障降级为单机模式，适合平滑迁移；
  **生产多副本请置 true**，让 Redis 故障显式失败而非静默脑裂。

### Bug Fixes

* 修复 `reload()` 拆分后 `_activate_workload()` 引用未定义局部变量
  `discovery_clients` / `gateway_clients` 导致应用启动失败
  （真实启动验证时发现），改用 `get_*_client()`。

### Tests

* 新增 10 项选举器单元测试（fake Redis）：抢锁、follower 待命、
  TTL 到期接管、续约失败降级、Redis 不可用降级/报错、
  停机释放租约、不误删他人锁、状态快照字段。
* 新增 `tests/integration/cluster_verify.sh` + `cluster_scenario.py`：
  真实 Redis 验证 6 组场景，全通过。
* 新增 `tests/integration/cluster_app_smoke.py`：启动两个真实应用实例，验证
  leader 有订阅/作业、follower 零订阅零作业、SIGKILL leader 后接管并重新装配。
  实测接管耗时 6.1s（TTL=6s）。
* 单机模式应用冒烟 9/9 通过，无回归（RSS +0.8%，线程 0 增长）。
* 静态依赖检查白名单化：`redis` 仅允许在 `app/runtime/cluster.py` 延迟导入。

## [3.0.0] - 2026-11-05

这是一次架构级重写：**同步机制从定时轮询改为订阅上游推送**，并**移除 funboost 与 Redis 依赖**，
一个容器即可运行。同时补齐 Nacos 1/2/3、APISIX 2/3、Kong 2/3 的多版本适配。

### ⚠️ Breaking Changes

* **移除 Redis 依赖**：不再需要 `REDIS_HOST` / `REDIS_URL` 等环境变量，无需部署 Redis。
  原先依赖 Redis 的任务持久化能力由「订阅事件去抖 + 5 分钟全量对账兜底」替代。
* **单副本约束**：服务持有内存客户端字典、内存订阅管理器、UDP socket 与 SQLite 任务库，
  且去掉了 Redis 分布式锁，**必须以单副本运行**（K8s `replicas: 1`）。
  多副本 HA 需外置状态并引入 leader 选举，属另一量级改造。
* **`/-/reload` 语义变更**：不再隐式触发重载，改为显式向本地任务总线提交 reload 任务。

### Features

* **订阅式同步**：新增 `app/subscription/` 订阅层，上游变更事件 → 去抖合并 → 只同步变更的单个服务，
  不再每次全量拉取注册中心与网关做 diff。
* **多策略 Provider**：Nacos 1.x 原生 UDP 推送、Nacos 2/3.x gRPC（可注入流）、
  快照 diff 增量轮询、入站 Webhook，按版本自动选择，UDP/gRPC 不可达时自动降级。
* **多版本能力矩阵**：新增 `app/compat/`，自动探测并适配
  Nacos 1/2/3（含 3.x 全新 `/nacos/v3/*` API、默认命名空间 `public`）、
  APISIX 2/3（响应形态归一化）、Kong 2/3（`POST` vs `PUT` target 语义差异）。
* **新增运维接口**：`POST /-/notify` 入站变更回调、`GET /-/subscriptions` 订阅状态、
  `GET /-/versions` 版本探测结果。
* **备份工具支持 kubectl port-forward**：`tools/backup.py` 可直接隧道连接 K8s 集群内的 syncer。
* **备份支持多集群多 kubeconfig**：`clusters` + `jobs` 配置结构，一个 job 批量备份多个集群，
  每集群可指定独立 kubeconfig 文件。
* **CI/CD**：新增 `release.yml` 发版工作流（测试通过 → 打 tag → 写 CHANGELOG → 推 gcr.io + Docker Hub）、
  `ci.yml` 快速反馈、`scripts/gen_changelog.py` 自动生成 CHANGELOG。

### Bug Fixes

* 修复 `FastAPI(**FASTAPI_CFG)` 传入不存在的 `env` 参数导致应用无法启动（该问题在 2.x 就已存在）。
* 修复 Kong 3.x 的 `PUT /targets/{target}` 对不存在的 target 返回 404（无 upsert），
  改为「先 POST，409 再 PUT」。
* 修复 `subscribe.mode: poll` 时 webhook 未注册导致 `/-/notify` 失效。
* 修复 `/health` 仅在「有 diff」时刷新 `Jobs.last_time` 导致订阅模式下误判失联，
  改为合并订阅心跳；空服务列表时也刷新心跳。
* 修复多 target 的 `health-check` 作业 id 固定互相覆盖，加 target 后缀。
* 修复 `Apisix`/`Kong` 缺少 `detect_version()` 导致 `/-/versions` 显示 unknown。

### Performance Improvements

* **客户端连接复用**：nacos/apisix/kong/eureka 改用持久 `httpx.Client` 连接池。
  旧实现每个请求新建 TCP 连接，订阅模式事件驱动会放大请求量，高 QPS 时快速耗尽本地端口。
* **事件队列有界化**：`EventBus` 队列加 `maxsize=10000`，满时丢弃并计入 `dropped` 而非阻塞推送线程，
  避免上游突发推送导致内存无界增长（丢弃安全由 5 分钟全量对账兜底）。
* **事件投递 O(1) 查找**：新增 target 索引，替代每个事件线性扫描全部 target。
* `show-memory` 接口改用 `heapq.nlargest` 流式取 top-N 并限制上限，
  旧实现把所有对象堆进列表再排序，内存翻倍。

### Refactoring

* 移除 funboost 与 Redis，新增 `app/runtime/`：进程内 `TaskBus`（ThreadPoolExecutor + 重试 + 统计）
  与 `LocalScheduler`（APScheduler + MemoryJobStore）。
* 任务上下文由 `funboost_current_task` / `TaskIdLogger` 改为 `ContextVar` + `TaskIdFilter`。
* 抽出 `app/service/sync_core.py`，统一 diff 计算与单服务同步逻辑，供事件驱动与全量对账复用。
* 重写 `tools/backup.py`：补齐类型注解、结构化日志、异常处理；
  修复 `restore.py` 生成的还原函数从不被调用的问题。

### Tests

* 新增 50 项单元测试（版本探测、订阅去抖、快照 diff、运行时、配置兼容、静态 import 回归）。
* 交付 Docker 多版本镜像矩阵（`tests/integration/`）与真实应用端到端冒烟脚本。
* 在 WSL + Docker 真实镜像验证：Nacos 1.4.6/2.5.5/3.3.0、APISIX 2.15.0/3.9.0、Kong 2.8.1/3.7.1。

## [2.7.8]

历史版本，见 Git 标签 `v2.7.8`。