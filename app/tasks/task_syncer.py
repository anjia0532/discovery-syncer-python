import functools
import importlib
import re

import nb_log
from apscheduler.triggers.cron import CronTrigger
from funboost import boost, funboost_aps_scheduler
from nb_time import NbTime

from app.model.syncer_model import Jobs
from app.service.discovery.discovery import Discovery
from app.service.gateway.gateway import Gateway
from app.tasks.common import FunboostCommonConfig
from core.database import db

logger = nb_log.get_logger(__name__)

"""
    创建生产者
    booster = BoostersManager.build_booster(BoosterParams(queue_name="queue_name", qps=0.2, consuming_function=add))
    发布消息
    BoostersManager.get_or_create_booster_by_queue_name("queue_name").publish()
"""


@functools.lru_cache()
def get_discovery_client(name: str) -> Discovery:
    """
    @param name: 注册中心名称
    @return: 注册中心实例
    """
    discovery_clients = importlib.import_module("app.model.config").discovery_clients
    discovery_client = discovery_clients.get(name)
    return discovery_client


@functools.lru_cache()
def get_gateway_client(name: str) -> Gateway:
    """
    @param name: 网关名称
    @return: 网关实例
    """
    gateway_clients = importlib.import_module("app.model.config").gateway_clients
    gateway_client = gateway_clients.get(name)
    return gateway_client


def clear_client():
    funboost_aps_scheduler.remove_all_jobs()

    importlib.import_module("app.model.config").discovery_clients.clear()
    get_discovery_client.cache_clear()

    importlib.import_module("app.model.config").gateway_clients.clear()
    get_gateway_client.cache_clear()

    enginex, sqla_helper = db.get_sqla_helper()
    Jobs.create_table_if_not_exists(sqla_helper)
    Jobs.clear_all(sqla_helper)


@boost(boost_params=FunboostCommonConfig(queue_name='queue_syncer_job', qps=50, ))
def syncer(target: dict):
    """
    @param target:
    @return:
    """
    discovery_client = get_discovery_client(target.get("discovery"))
    gateway_client = get_gateway_client(target.get("gateway"))

    if not discovery_client or not gateway_client:
        logger.warning(f"没有获取到注册中心或者网关实例{target}")
        return
    services = discovery_client.get_all_service(target.get("config"))
    if len(services) == 0:
        logger.warning(f"没有获取到服务列表{target}")
        return

    logger.info(f"同步服务列表, 作业: {target.get('id')} services: {services}")
    for service in services:
        # 如果服务名称在排除列表中，则跳过
        exclude = any(bool(re.match(ex, service.name)) for ex in target.get("exclude_service", []))
        if exclude:
            continue
        # 同步服务的所有实例
        discovery_instances = service.instances
        if not discovery_instances:
            discovery_instances, last_time = discovery_client.get_service_all_instances(service.name,
                                                                                        target.get("config"))
            service.last_time = last_time > 0 and last_time or int(NbTime().timestamp)
        logger.info(
            f"同步服务实例, 作业: {target.get('id')}, service_name: {service.name}, 最后更新时间为: {NbTime(service.last_time).datetime_str} ,instances: {discovery_instances}")

        gateway_instances = gateway_client.get_service_all_instances(target, service.name)
        logger.info(
            f"网关实例列表, 作业: {target.get('id')}, service_name: {service.name}, instances: {gateway_instances}")
        # key 为 注册中心实例 ip:port，value 为实例信息
        dim = {f"{item.ip}:{item.port}": item for item in (discovery_instances or [])}
        # key 为 网关实例 ip:port，value 为实例信息
        gim = {f"{item.ip}:{item.port}": item for item in (gateway_instances or [])}
        # 合并两个字典，获取合集
        merged_dict = {**gim, **dim}
        # 获取差集
        diffIns = [item._replace(change=True, enabled=key in dim) for key, item in merged_dict.items() if
                   key not in dim or key not in gim or dim[key].weight != gim[key].weight]
        logger.info(f"获取变更实例列表, 作业: {target.get('id')}, service_name: {service.name}, instances: {diffIns}")
        # 同步差异
        if not diffIns:
            logger.info(f"没有变更实例,跳过更新, 作业: {target.get('id')}, service_name: {service.name}")
            continue
        gateway_client.sync_instances(target, service.name, diffIns, discovery_instances)
        Jobs(**target).save_or_update(db.get_sqla_helper()[1])


@boost(boost_params=FunboostCommonConfig(queue_name='queue_reload_job', qps=1, ))
def reload():
    logger.info("load config yaml")
    settings = importlib.import_module("app.model.config").settings
    clear_client()
    # key 为 name，value 为 discovery client
    discovery_clients = importlib.import_module("app.model.config").discovery_clients
    if settings.config.discovery_servers:
        for name, discovery in settings.config.discovery_servers.items():
            cls = getattr(importlib.import_module(f"app.service.discovery.{discovery.type.value}"),
                          discovery.type.value.title())
            client = cls(discovery)
            discovery_clients[name] = client
    # key 为 name，value 为 gateway client
    gateway_clients = importlib.import_module("app.model.config").gateway_clients
    if settings.config.gateway_servers:
        for name, gateway in settings.config.gateway_servers.items():
            cls = getattr(importlib.import_module(f"app.service.gateway.{gateway.type.value}"),
                          gateway.type.value.title())
            client = cls(gateway)
            gateway_clients[name] = client
    sqla_helper = db.get_sqla_helper()[1]
    if settings.config.targets:
        for index, target in enumerate(settings.config.targets):
            target.id = f"{index}-{target.gateway}-{target.discovery}"
            values = target.fetch_interval.split()
            assert len(values) == 6, f"fetch_interval格式错误{target.fetch_interval}"
            if target.enabled:
                Jobs(**target.dict()).save_or_update(sqla_helper)
                trigger = CronTrigger(second=values[0], minute=values[1], hour=values[2], day=values[3],
                                      month=values[4], day_of_week=values[5])
                funboost_aps_scheduler.add_push_job(syncer, id=target.id, name=target.id, trigger=trigger,
                                                    kwargs={"target": target.model_dump()}, replace_existing=True)
