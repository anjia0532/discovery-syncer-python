"""同步任务编排（订阅化重构版）。

架构变化
--------
旧：``reload()`` 为每个 target 注册 CronTrigger -> 定时 ``syncer()``
    全量拉注册中心 + 全量拉网关 -> 比对差异 -> 同步。

新：``reload()`` 装配 :class:`SubscriptionManager` -> 上游变更事件
    -> 去抖合并 -> ``sync_service()`` 只同步变更的**单个服务**；
    ``syncer()`` 保留为低频全量对账兜底（以及 ``subscribe.mode: off`` 时的旧行为）。

兼容性
------
* 旧 ``config.yaml`` 不写任何订阅字段即可加载，默认 AUTO 订阅 + 兜底对账。
* ``fetch-interval`` / ``maximum-interval-sec`` / ``healthcheck`` / ``config.template``
  等字段语义保持不变（前者转为对账/健康检查周期）。
* ``time_parser`` 仍从本模块导出，外部 import 不破坏。
"""

from __future__ import annotations

import functools
import importlib
import json
import re
from typing import List, Optional
from urllib.parse import urlparse

from apscheduler.triggers.cron import CronTrigger
from funboost import boost, funboost_current_task
from funboost.timing_job.apscheduler_use_redis_store import funboost_background_scheduler_redis_store

from app.model.syncer_model import DiscoveryInstance, Jobs, Registration
from app.service.discovery.discovery import Discovery
from app.service.gateway.gateway import Gateway
from app.service.sync_core import SyncResult, sync_one_service
from app.subscription import SubscribeMode as RuntimeSubscribeMode
from app.subscription import SubscriptionManager, TargetSpec
from app.subscription.cron import time_parser
from app.tasks.common import FunboostCommonConfig
from core.database import db
from core.lib.logger import for_task

logger = for_task(__name__)

# 订阅模式下未显式配置 reconcile-interval 时使用的兜底对账周期。
# 不能复用 fetch-interval（旧配置常为 10s），否则等于又变回高频全量轮询。
DEFAULT_RECONCILE_INTERVAL = "@every 5m"

_manager: Optional[SubscriptionManager] = None


# ---------------------------------------------------------------------------
# 订阅管理器（延迟创建，避免 import 期连接 redis）
# ---------------------------------------------------------------------------
def _find_target(target_id: str) -> Optional[dict]:
    """按 id 找到 target 配置并转成可序列化 dict（供事件投递）。"""
    from app.model.config import settings

    for target in (settings.config.targets or []):
        if target.id == target_id:
            return target.model_dump()
    return None


def _publish_sync_service(target_id: str, service_name: str, event: Any) -> None:
    """EventBus 的 publisher：把变更事件投进 funboost 队列。"""
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
    sync_service.publish(payload)


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
# 客户端缓存
# ---------------------------------------------------------------------------
@functools.lru_cache()
def get_discovery_client(name: str) -> Discovery:
    """按名称取注册中心客户端。"""
    from app.model.config import discovery_clients

    if not discovery_clients:
        reload.publish(msg={})
    return discovery_clients.get(name)


@functools.lru_cache()
def get_gateway_client(name: str) -> Gateway:
    """按名称取网关客户端。"""
    from app.model.config import gateway_clients

    if not gateway_clients:
        reload.publish(msg={})
    return gateway_clients.get(name)


def clear_client():
    """清空定时任务、客户端缓存与运行态，并停止所有订阅 Provider。"""
    funboost_background_scheduler_redis_store.remove_all_jobs()
    if _manager is not None:
        _manager.stop_all()
    from app.model.config import discovery_clients, gateway_clients

    discovery_clients.clear()
    get_discovery_client.cache_clear()
    gateway_clients.clear()
    get_gateway_client.cache_clear()

    _engine, sqla_helper = db.get_sqla_helper()
    Jobs.create_table_if_not_exists(sqla_helper)
    Jobs.clear_all(sqla_helper)


# ---------------------------------------------------------------------------
# 健康检查（沿用原有逻辑，抽成 hook 供 sync_core 复用）
# ---------------------------------------------------------------------------
def _apply_healthcheck(target: dict, service_name: str, instances: List[Any]) -> List[Any]:
    """结合主动健康检查状态过滤注册中心实例，并下线 unhealthy 节点。

    这段逻辑与旧 ``syncer()`` 完全一致，只是从大循环里抽出来，
    使得订阅事件触发的单服务同步也能复用。

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
# 任务：单服务事件同步 / 全量对账 / 健康检查 / reload
# ---------------------------------------------------------------------------
@boost(boost_params=FunboostCommonConfig(queue_name='queue_instance_health_check', qps=100))
def instance_health_check(target: dict, instance: dict):
    sqla_helper = db.get_sqla_helper()[1]
    healthcheck = target.get("healthcheck", None)
    discovery_instance = DiscoveryInstance(instance)
    try:
        discovery_instance.health_check(healthcheck=healthcheck, sqla_helper=sqla_helper)
    except Exception as e:
        logger.warning(f"健康检查报错 {e.args}")


@boost(boost_params=FunboostCommonConfig(queue_name='queue_health_check_job', qps=50, ))
def health_check(target: dict):
    healthcheck = target.get("healthcheck", None)
    target_id = target.get("id", None)
    if not target_id or not healthcheck:
        return
    sqla_helper = db.get_sqla_helper()[1]
    instances = DiscoveryInstance({}).get_instances_by_target_id(target_id, sqla_helper)
    if not instances:
        return
    fct = funboost_current_task()
    for instance in instances:
        instance_health_check.publish({'target': target, 'instance': instance.to_dict_item()},
                                      task_id=fct.function_result_status.task_id)


@boost(boost_params=FunboostCommonConfig(queue_name='queue_sync_service_job', qps=100, ))
def sync_service(target: dict, service_name: str, event: dict = None):
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


@boost(boost_params=FunboostCommonConfig(queue_name='queue_syncer_job', qps=50, ))
def syncer(target: dict):
    """全量对账：遍历所有服务，仅对有差异的做同步。

    保留该任务以兼容旧调度与 ``subscribe.mode: off``；订阅模式下它由
    ``reconcile-interval`` 低频触发，作为防丢事件的兜底，而不再是取差异的主路径。
    """
    discovery_client = get_discovery_client(target.get("discovery"))
    gateway_client = get_gateway_client(target.get("gateway"))
    if not discovery_client or not gateway_client:
        logger.warning(f"没有获取到注册中心或者网关实例{target}")
        return
    services = discovery_client.get_all_service(target.get("config"))
    if len(services) == 0:
        logger.info(f"没有获取到服务列表{target}")
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
    """把定时表达式粗略换算成秒，用于订阅续约/轮询线程间隔。

    支持 ``@every 10s|5m|1h`` 与 ``*/N * * * * *``；其它 cron 表达式返回默认值。

    Args:
        expr: 定时表达式。
        default: 无法换算时的默认值。

    Returns:
        秒数。
    """
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
    # Nacos 2.x 官方订阅是 gRPC，但服务端保留 UDP 兼容层；未注入 gRPC 流时
    # 先走 UDP 兼容层，探测不通再由 Manager 自动降级为快照轮询。
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
    """注册定时作业（兼容 @reboot 与 5/6 位 cron）。"""
    second, minute, hour, day, month, day_of_week, next_run_time = time_parser(expr)
    if next_run_time:
        funboost_background_scheduler_redis_store.add_push_job(
            func, id=job_id, name=job_id, next_run_time=next_run_time,
            kwargs=kwargs, replace_existing=True)
    else:
        funboost_background_scheduler_redis_store.add_push_job(
            func, id=job_id, name=job_id,
            trigger=CronTrigger(second=second, minute=minute, hour=hour,
                                day=day, month=month, day_of_week=day_of_week),
            kwargs=kwargs, replace_existing=True)


@boost(boost_params=FunboostCommonConfig(queue_name='queue_reload_job', qps=1, ))
def reload():
    """重新加载配置：构建客户端、探测版本、装配订阅、注册兜底对账与健康检查。"""
    logger.info("load config yaml")
    from app.model.config import discovery_clients, gateway_clients, settings

    clear_client()

    if settings.config.discovery_servers:
        for name, discovery in settings.config.discovery_servers.items():
            cls = getattr(importlib.import_module(f"app.service.discovery.{discovery.type.value}"),
                          ''.join([k.title() for k in discovery.type.value.split('_')]))
            discovery_clients[name] = cls(discovery)
    if settings.config.gateway_servers:
        for name, gateway in settings.config.gateway_servers.items():
            cls = getattr(importlib.import_module(f"app.service.gateway.{gateway.type.value}"),
                          ''.join([k.title() for k in gateway.type.value.split('_')]))
            gateway_clients[name] = cls(gateway)

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
            spec = _build_spec(target, discovery_clients.get(target.discovery),
                               gateway_clients.get(target.gateway))
            if spec:
                specs.append(spec)
            # 订阅为主；全量对账降级为低频兜底（默认 5 分钟）。
            if sub.reconcile:
                _register_cron(target, syncer,
                               sub.reconcile_interval or DEFAULT_RECONCILE_INTERVAL,
                               f"reconcile-{target.id}", payload)
        else:
            # OFF：完全回退旧版定时全量轮询，便于灰度对比。
            _register_cron(target, syncer, target.fetch_interval, target.id, payload)

        # 健康检查沿用原周期；id 加 target 后缀，修复原实现多 target 互相覆盖的 bug。
        if target.healthcheck:
            second, minute, hour, day, month, day_of_week, next_run_time = time_parser(target.fetch_interval)
            if not next_run_time:
                funboost_background_scheduler_redis_store.add_push_job(
                    health_check, id=f"health-check-{target.id}", name=f"health-check-{target.id}",
                    trigger=CronTrigger(second=second, minute=minute, hour=hour,
                                        day=day, month=month, day_of_week=day_of_week),
                    kwargs=payload, replace_existing=True)

    debounce = enabled_targets[0].subscribe.debounce_ms
    manager = get_manager(debounce)
    summary = manager.rebuild(specs)
    logger.info("订阅装配完成: %s", json.dumps(
        {k: {"version": v["version"], "push": v["push_transport"],
             "providers": [p["kind"] for p in v["providers"]]} for k, v in summary.items()},
        ensure_ascii=False))
