"""同步任务编排（订阅化 + 本地运行时版）。

架构演进
--------
1. 旧版：``reload()`` 注册 CronTrigger -> 定时 ``syncer()`` 全量拉两边比对。
2. 订阅化：``reload()`` 装配 SubscriptionManager -> 事件 -> ``sync_service()``
   只同步变更的单个服务；``syncer()`` 保留为低频全量对账兜底。
3. 本版：移除 funboost + Redis，任务执行与定时调度全部改为**进程内**：
   ``app.runtime.TaskBus``（ThreadPoolExecutor）与 ``app.runtime.LocalScheduler``
   （APScheduler + MemoryJobStore）。

兼容性
------
* 旧 ``config.yaml`` 不写任何订阅字段即可加载，默认 AUTO 订阅 + 兜底对账。
* ``fetch-interval`` / ``maximum-interval-sec`` / ``healthcheck`` / ``config.template``
  等字段语义保持不变。
* ``time_parser`` 仍从本模块导出，外部 import 不破坏。

单副本约束
----------
本服务持有内存客户端字典、内存订阅管理器、UDP socket 与 SQLite 任务库，
且去掉了 Redis 分布式锁，必须以单副本运行（K8s ``replicas: 1``）。
"""

from __future__ import annotations

import importlib
import json
import re
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from app.model.syncer_model import DiscoveryInstance, Jobs, Registration
from app.runtime import get_scheduler, get_task_bus
from app.service.discovery.discovery import Discovery
from app.service.gateway.gateway import Gateway
from app.service.sync_core import SyncResult, sync_one_service
from app.subscription import SubscribeMode as RuntimeSubscribeMode
from app.subscription import SubscriptionManager, TargetSpec
from app.subscription.cron import time_parser
from core.database import db
from core.lib.logger import for_task
from core.lib.task_context import current_task

logger = for_task(__name__)

# 订阅模式下未显式配置 reconcile-interval 时使用的兜底对账周期。
# 不能复用 fetch-interval（旧配置常为 10s），否则等于又变回高频全量轮询。
DEFAULT_RECONCILE_INTERVAL = "@every 5m"

_manager: Optional[SubscriptionManager] = None
_clients_lock = threading.Lock()
_reloading = False
# target_id -> 可序列化 dict，供事件投递 O(1) 查找（reload 时重建）
_target_index: Dict[str, dict] = {}


# ---------------------------------------------------------------------------
# 订阅管理器
# ---------------------------------------------------------------------------
def _find_target(target_id: str) -> Optional[dict]:
    """按 id 找到 target 配置并转成可序列化 dict（供事件投递）。

    每次事件都会调用，因此用 ``reload`` 时重建的索引避免线性扫描。
    索引缺失时回退到线性查找，保证正确性。
    """
    from app.model.config import settings

    cached = _target_index.get(target_id)
    if cached is not None:
        return cached

    for target in (settings.config.targets or []):
        if target.id == target_id:
            return target.model_dump()
    return None


def _rebuild_target_index() -> None:
    """在 ``reload`` 末尾重建 target 索引，供事件投递 O(1) 查找。"""
    from app.model.config import settings

    index = {t.id: t.model_dump() for t in (settings.config.targets or []) if t.id}
    _target_index.clear()
    _target_index.update(index)


def _publish_sync_service(target_id: str, service_name: str, event: Any) -> None:
    """EventBus 的 publisher：把变更事件提交给本地任务总线。"""
    target = _find_target(target_id)
    if not target:
        logger.warning("事件找不到对应 target, id=%s service=%s", target_id, service_name)
        return
    payload = {
        "target": target,
        "service_name": service_name,
        "event": {
            "service_name": event.service_name,
            "discovery": event.discovery,
            "group": event.group,
            "namespace": event.namespace,
            "source": event.source,
            "ts": event.ts,
        },
    }
    try:
        get_task_bus().submit(sync_service, **payload)
    except RuntimeError:
        # 停机竞态：shutdown 已关闭 TaskBus，此时直接丢弃事件。
        logger.debug("TaskBus 已关闭，丢弃事件 target=%s service=%s", target_id, service_name)


def get_manager(debounce_ms: Optional[int] = None) -> SubscriptionManager:
    """取得全局订阅管理器。

    只有**显式传入**且不同的 debounce 才重建；无参调用必须复用现有实例，
    否则事件处理路径上的 ``get_manager()`` 会意外清空已装配的订阅。
    """
    global _manager
    if _manager is None:
        _manager = SubscriptionManager(publisher=_publish_sync_service,
                                       debounce_ms=debounce_ms if debounce_ms is not None else 500)
    elif debounce_ms is not None and _manager._debounce_ms != debounce_ms:  # noqa: SLF001
        _manager.stop_all()
        _manager = SubscriptionManager(publisher=_publish_sync_service, debounce_ms=debounce_ms)
    return _manager


# ---------------------------------------------------------------------------
# 客户端缓存与初始化
# ---------------------------------------------------------------------------
def _ensure_clients() -> None:
    """客户端字典为空时同步完成一次 reload。

    原实现在这里 ``reload.publish(msg={})``，靠消息队列的**异步**特性规避
    了「reload 内部又访问 config」的重入。改成本地同步调用后必须显式加
    重入保护，否则会递归。``reload()`` 本身只直接读 ``discovery_clients``
    字典，不走 ``get_*_client()``，因此正常路径不会触发二次进入。
    """
    global _reloading
    from app.model.config import discovery_clients, gateway_clients

    if discovery_clients and gateway_clients:
        return
    with _clients_lock:
        if _reloading or (discovery_clients and gateway_clients):
            return
        _reloading = True
        try:
            logger.warning("客户端尚未初始化，触发一次同步 reload")
            reload()
        finally:
            _reloading = False


def get_discovery_client(name: str) -> Discovery:
    """按名称取注册中心客户端。

    刻意不用 ``lru_cache``：字典查找本身 O(1)，缓存没有收益，反而会
    把「启动未完成时的 None」永久缓存住（后续 reload 也不会刷新）。
    """
    from app.model.config import discovery_clients

    _ensure_clients()
    return discovery_clients.get(name)


def get_gateway_client(name: str) -> Gateway:
    """按名称取网关客户端。理由同 :func:`get_discovery_client`。"""
    from app.model.config import gateway_clients

    _ensure_clients()
    return gateway_clients.get(name)


def clear_client() -> None:
    """清空定时任务、客户端缓存与运行态，并停止所有订阅 Provider。"""
    get_scheduler().remove_all()
    if _manager is not None:
        _manager.stop_all()
    from app.model.config import discovery_clients, gateway_clients

    # 关闭旧客户端持有的 httpx 连接池，否则每次 reload 都会泄漏一批连接。
    for client in list(discovery_clients.values()) + list(gateway_clients.values()):
        closer = getattr(client, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass

    discovery_clients.clear()
    gateway_clients.clear()
    _target_index.clear()

    _engine, sqla_helper = db.get_sqla_helper()
    Jobs.create_table_if_not_exists(sqla_helper)
    Jobs.clear_all(sqla_helper)


# ---------------------------------------------------------------------------
# 健康检查（沿用原有逻辑，抽成 hook 供 sync_core 复用）
# ---------------------------------------------------------------------------
def _apply_healthcheck(target: dict, service_name: str, instances: List[Any]) -> List[Any]:
    """结合主动健康检查状态过滤注册中心实例，并下线 unhealthy 节点。

    Args:
        target: target 配置字典。
        service_name: 服务名。
        instances: 注册中心实例列表。

    Returns:
        过滤后的实例列表。
    """
    healthcheck = target.get("healthcheck") or {}
    if not healthcheck:
        return instances
    sqla_helper = db.get_sqla_helper()[1]
    discovery_client = get_discovery_client(target.get("discovery"))
    try:
        DiscoveryInstance({"target_id": target.get('id'), "service": service_name}).save_or_update(
            instances, sqla_helper)
        tracked = DiscoveryInstance(
            {"target_id": target.get('id'), "service": service_name}).get_target_service_all_instance(
            healthcheck.get("min-hosts", 1), sqla_helper)
        unhealthy = [d for d in tracked if d.status == "unhealthy"]
        if not unhealthy:
            return instances
        unhealthy_keys = [b.instance for b in unhealthy]
        down_instances = [d for d in instances if f"{d.ip}:{d.port}" in unhealthy_keys]
        instances = [d for d in instances if f"{d.ip}:{d.port}" not in unhealthy_keys]
        if down_instances:
            for item in down_instances:
                item.change = True
                item.enabled = False
            registration = Registration(service_name=service_name, ext_data=target.get("config", {}))
            discovery_client.modify_registration(registration, down_instances)
        DiscoveryInstance({}).delete_by_instances(unhealthy_keys, sqla_helper)
    except Exception as exc:  # noqa: BLE001
        logger.warning("健康检查下线实例失败, %s , %s", target.get('id'), service_name, exc_info=exc)
    return instances


# ---------------------------------------------------------------------------
# 任务函数（普通函数，由 TaskBus 提交；不再有 @boost 装饰器）
# ---------------------------------------------------------------------------
def instance_health_check(target: dict, instance: dict) -> None:
    """对单个实例执行一次主动健康检查。"""
    sqla_helper = db.get_sqla_helper()[1]
    healthcheck = target.get("healthcheck", None)
    discovery_instance = DiscoveryInstance(instance)
    try:
        discovery_instance.health_check(healthcheck=healthcheck, sqla_helper=sqla_helper)
    except Exception as e:
        logger.warning(f"健康检查报错 {e.args}")


def health_check(target: dict) -> None:
    """把 target 下所有实例分发成单实例检查任务。"""
    healthcheck = target.get("healthcheck", None)
    target_id = target.get("id", None)
    if not target_id or not healthcheck:
        return
    sqla_helper = db.get_sqla_helper()[1]
    instances = DiscoveryInstance({}).get_instances_by_target_id(target_id, sqla_helper)
    if not instances:
        return
    context = current_task()
    parent_task_id = context.task_id if context else None
    for instance in instances:
        get_task_bus().submit(
            instance_health_check,
            task_id=parent_task_id,
            target=target,
            instance=instance.to_dict_item(),
        )


def sync_service(target: dict, service_name: str, event: dict = None) -> None:
    """事件驱动：只同步一个发生变更的服务。

    Args:
        target: target 配置字典。
        service_name: 上游变更的服务名。
        event: 变更事件元信息（来源/时间），仅用于日志与排障。
    """
    discovery_client = get_discovery_client(target.get("discovery"))
    gateway_client = get_gateway_client(target.get("gateway"))
    if not discovery_client or not gateway_client:
        logger.warning(f"没有获取到注册中心或者网关实例{target}")
        return
    result: SyncResult = sync_one_service(
        target, service_name, discovery_client, gateway_client,
        healthcheck_hook=_apply_healthcheck,
        on_heartbeat=get_manager().heartbeat,
        log=logger,
    )
    logger.info("事件同步完成 source=%s target=%s result=%s",
                (event or {}).get("source", "unknown"), target.get("id"), result.to_dict())
    if result.changed:
        Jobs(**target).save_or_update(db.get_sqla_helper()[1])


def syncer(target: dict) -> None:
    """全量对账：遍历所有服务，仅对有差异的做同步。

    保留该任务以兼容旧调度与 ``subscribe.mode: off``；订阅模式下它由
    ``reconcile-interval`` 低频触发，作为防丢事件的兜底。
    """
    discovery_client = get_discovery_client(target.get("discovery"))
    gateway_client = get_gateway_client(target.get("gateway"))
    if not discovery_client or not gateway_client:
        logger.warning(f"没有获取到注册中心或者网关实例{target}")
        return
    services = discovery_client.get_all_service(target.get("config"))
    if len(services) == 0:
        # 上游暂无服务也算「对账成功完成」，必须刷新心跳，
        # 否则空集群/全服务下线会被 /health 误判为失联。
        logger.info(f"没有获取到服务列表{target}")
        get_manager().heartbeat(target.get("id") or "")
        return

    logger.info(f"全量对账服务列表, 作业: {target.get('id')} services: {[s.name for s in services]}")
    sqla_helper = db.get_sqla_helper()[1]
    for service in services:
        result = sync_one_service(
            target, service.name, discovery_client, gateway_client,
            healthcheck_hook=_apply_healthcheck,
            on_heartbeat=get_manager().heartbeat,
            log=logger,
            prefetched_instances=service.instances,
        )
        if result.changed:
            Jobs(**target).save_or_update(sqla_helper)
    get_manager().heartbeat(target.get("id") or "")


# ---------------------------------------------------------------------------
# 版本适配 + 订阅装配
# ---------------------------------------------------------------------------
def _interval_seconds(expr: str, default: float = 30.0) -> float:
    """把定时表达式粗略换算成秒，用于订阅续约/轮询线程间隔。"""
    text = expr or ""
    match = re.search(r"@every\s+(\d+)\s*([smh])", text, re.I)
    if match:
        return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2).lower()]
    match = re.match(r"\*/(\d+)\s+\*\s+\*\s+\*\s+\*\s+\*", text.strip())
    if match:
        return float(match.group(1))
    return default


def _mode_of(target) -> RuntimeSubscribeMode:
    """把配置层枚举映射到运行层枚举，并兼容未启用订阅的旧配置。"""
    sub = target.subscribe
    if not sub.enabled:
        return RuntimeSubscribeMode.OFF
    return RuntimeSubscribeMode(sub.mode.value)


def _build_spec(target, discovery_client, gateway_client) -> Optional[TargetSpec]:
    """按注册中心/网关版本能力构造订阅装配参数。"""
    from app.compat import PushTransport

    sub = target.subscribe
    cap = discovery_client.detect_version() if discovery_client else None
    gateway_cap = gateway_client.capability if gateway_client else None
    if gateway_client is not None and gateway_cap is None:
        gateway_cap = getattr(gateway_client, "capability", None)

    major = cap.major if cap else 1
    label = cap.label if cap else "unknown"
    evidence = getattr(discovery_client, "version_evidence", "") if discovery_client else ""

    transport = cap.push.value if cap else "poll"
    if cap and cap.major == 2:
        transport = PushTransport.UDP.value
    if discovery_client is not None and not discovery_client.supports_push:
        transport = "poll"

    cfg_obj = getattr(discovery_client, "_config", None) if discovery_client else None
    host = (getattr(cfg_obj, "host", "") if cfg_obj else "") or ""
    parsed = urlparse(host)
    main_port = parsed.port or (443 if parsed.scheme == "https" else 8848)
    grpc_port = main_port + (cap.grpc_offset if cap else 0)
    grpc_probe = None
    if cap and cap.grpc_offset:
        from app.compat import probe_tcp

        grpc_probe = lambda h, p: probe_tcp(h, p, 1.0)  # noqa: E731

    refresh_expr = sub.refresh_interval or target.fetch_interval
    refresh_sec = _interval_seconds(refresh_expr, 30.0)
    poll_sec = _interval_seconds(target.fetch_interval, 10.0)
    push_timeout = float(sub.push_timeout_sec) or refresh_sec * 3

    cfg = target.config or {}
    list_services = (lambda: discovery_client.get_all_service(cfg)) if discovery_client else (lambda: [])
    send_subscribe = None
    if discovery_client and transport == "udp":
        def _send(name: str, group: str, namespace: str, udp_port: int) -> None:
            discovery_client.subscribe_instance(name, group, namespace, udp_port)

        send_subscribe = _send

    return TargetSpec(
        target_id=target.id,
        discovery=target.discovery,
        mode=_mode_of(target),
        push_transport=transport,
        major=major,
        label=label,
        evidence=evidence,
        list_services=list_services,
        send_subscribe=send_subscribe,
        snapshot_provider=list_services,
        grpc_probe=grpc_probe,
        grpc_host=parsed.hostname or "",
        grpc_port=grpc_port,
        refresh_interval_sec=refresh_sec,
        poll_interval_sec=poll_sec,
        push_timeout_sec=push_timeout,
        udp_host=sub.udp_host,
        udp_port=sub.udp_port,
        webhook_enabled=sub.webhook,
        fallback_enabled=sub.fallback,
    )


def _register_cron(target, func, expr: str, job_id: str, kwargs: dict) -> None:
    """把定时作业登记到本地调度器（兼容 @reboot 与 5/6 位 cron）。"""
    second, minute, hour, day, month, day_of_week, next_run_time = time_parser(expr)
    get_scheduler().add_cron(
        job_id=job_id,
        func=func,
        trigger_fields=None if next_run_time else {
            "second": second, "minute": minute, "hour": hour,
            "day": day, "month": month, "day_of_week": day_of_week,
        },
        next_run_time=next_run_time,
        kwargs=kwargs,
    )


def reload() -> None:
    """重新加载配置：构建客户端、初始化集群选举、按角色装配订阅与调度。

    集群模式下只有 leader 装配订阅/调度并写网关，follower 仅持有客户端待命；
    角色切换由 :func:`_on_cluster_role_change` 回调驱动重新装配。
    """
    logger.info("load config yaml")
    from app.model.config import discovery_clients, gateway_clients, settings

    clear_client()
    _build_clients(settings, discovery_clients, gateway_clients)

    # 集群选举：未启用时不触碰 Redis（单副本行为与 v3.0.0 完全一致）。
    cluster_cfg = getattr(settings.config, "cluster", None)
    from app.runtime import get_elector, init_cluster

    enabled = bool(cluster_cfg and cluster_cfg.enabled)
    if enabled:
        elector = init_cluster(cluster_cfg, on_role_change=_on_cluster_role_change)
        if elector is None:
            logger.warning("集群模式已启用但选举器初始化失败，按单机模式运行")
        else:
            state = elector.state
            logger.info("集群模式启动: role=%s instance=%s", state.role.value, state.instance_id)
            if state.role.value == "error":
                raise RuntimeError(f"集群选举失败: {state.last_error}")
    else:
        # 显式关闭：确保从集群模式切回单机时不残留旧选举器
        init_cluster(None)

    # 只有 leader（或未启用集群）才真正装配订阅与定时作业。
    if _is_active():
        _activate_workload()
    else:
        logger.info("当前副本为 follower，跳过订阅装配与定时作业（待命接管）")
        get_manager().rebuild([])
        get_scheduler().remove_all()


def _build_clients(settings_obj, discovery_clients: dict, gateway_clients: dict) -> None:
    """按配置构建注册中心/网关客户端（不做任何装配，角色无关）。"""
    if settings_obj.config.discovery_servers:
        for name, discovery in settings_obj.config.discovery_servers.items():
            cls = getattr(importlib.import_module(f"app.service.discovery.{discovery.type.value}"),
                          ''.join([k.title() for k in discovery.type.value.split('_')]))
            discovery_clients[name] = cls(discovery)
    if settings_obj.config.gateway_servers:
        for name, gateway in settings_obj.config.gateway_servers.items():
            cls = getattr(importlib.import_module(f"app.service.gateway.{gateway.type.value}"),
                          ''.join([k.title() for k in gateway.type.value.split('_')]))
            gateway_clients[name] = cls(gateway)


def _is_active() -> bool:
    """本副本是否应承担实际工作。

    未启用集群 / 未初始化选举器 → True（单机行为）。
    ``LeaderElector.is_leader`` 对 STANDALONE 也返回 True，
    因此 Redis 降级时也不会误停业务。
    """
    from app.runtime import get_elector

    elector = get_elector()
    if elector is None:
        return True
    return elector.is_leader


def _on_cluster_role_change(old, new) -> None:
    """角色变化回调：成为 leader 时装配，失去 leader 时停止装配。

    该回调在选举线程内被调用，装配涉及网络与线程创建，耗时可能到秒级；
    因此这里直接执行（选举线程本就不做别的重活），
    但**吞掉所有异常**，避免把选举线程搞死。
    """
    logger.warning("集群角色变化: %s -> %s，开始重新装配工作负载", old, new)
    try:
        if new.value in ("leader", "standalone"):
            _activate_workload()
        elif new.value in ("follower", "error"):
            _deactivate_workload()
    except Exception:  # noqa: BLE001 - 回调不能让选举线程崩溃
        logger.exception("角色切换后重新装配失败")


def _activate_workload() -> None:
    """装配订阅 Provider、兜底对账与健康检查（仅 leader 调用）。"""
    from app.model.config import settings

    sqla_helper = db.get_sqla_helper()[1]
    specs: List[TargetSpec] = []
    enabled_targets = [t for t in (settings.config.targets or []) if t.enabled]
    if not enabled_targets:
        get_manager().rebuild([])
        return

    for index, target in enumerate(settings.config.targets):
        target.id = f"{index}-{target.gateway}-{target.discovery}"
        if not target.enabled:
            continue
        Jobs(**target.model_dump()).save_or_update(sqla_helper)
        payload = {"target": target.model_dump()}
        sub = target.subscribe
        mode = _mode_of(target)

        if mode != RuntimeSubscribeMode.OFF:
            # 走 get_*_client()：内部带 _ensure_clients()，且能容忍
            # reload 重建字典的窗口期（直接读字典会拿到 None）。
            spec = _build_spec(target, get_discovery_client(target.discovery),
                               get_gateway_client(target.gateway))
            if spec:
                specs.append(spec)
            if sub.reconcile:
                _register_cron(target, syncer,
                               sub.reconcile_interval or DEFAULT_RECONCILE_INTERVAL,
                               f"reconcile-{target.id}", payload)
        else:
            _register_cron(target, syncer, target.fetch_interval, target.id, payload)

        if target.healthcheck:
            second, minute, hour, day, month, day_of_week, next_run_time = time_parser(target.fetch_interval)
            if not next_run_time:
                get_scheduler().add_cron(
                    job_id=f"health-check-{target.id}",
                    func=health_check,
                    trigger_fields={
                        "second": second, "minute": minute, "hour": hour,
                        "day": day, "month": month, "day_of_week": day_of_week,
                    },
                    kwargs=payload,
                )

    debounce = enabled_targets[0].subscribe.debounce_ms
    manager = get_manager(debounce)
    summary = manager.rebuild(specs)
    # 索引必须在订阅装配后重建：Provider 一旦启动就可能立刻推事件，
    # 而事件传递依赖 _find_target 拿到 target 配置。
    _rebuild_target_index()
    logger.info("订阅装配完成: %s", json.dumps(
        {k: {"version": v["version"], "push": v["push_transport"],
             "providers": [p["kind"] for p in v["providers"]]} for k, v in summary.items()},
        ensure_ascii=False))


def _deactivate_workload() -> None:
    """停止订阅 Provider 与全部定时作业（失去 leader 时调用）。

    只停「干活」的部分：保留客户端与任务总线，便于重新接管时快速恢复。
    """
    logger.warning("停止本副本的工作负载（订阅 + 定时作业）")
    try:
        get_manager().rebuild([])
    except Exception:  # noqa: BLE001
        logger.exception("停止订阅失败")
    try:
        get_scheduler().remove_all()
    except Exception:  # noqa: BLE001
        logger.exception("清空定时作业失败")
