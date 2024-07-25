import datetime
import functools
import importlib
import re

from apscheduler.triggers.cron import CronTrigger
from funboost import boost, funboost_aps_scheduler, funboost_current_task
from nb_time import NbTime

from app.model.syncer_model import Jobs, DiscoveryInstance, Registration
from app.service.discovery.discovery import Discovery
from app.service.gateway.gateway import Gateway
from app.tasks.common import FunboostCommonConfig
from core.database import db
from core.lib.logger import for_task

logger = for_task(__name__)

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
    from app.model.config import discovery_clients
    if not discovery_clients:
        reload.publish(msg={})
    discovery_client = discovery_clients.get(name)
    return discovery_client


@functools.lru_cache()
def get_gateway_client(name: str) -> Gateway:
    """
    @param name: 网关名称
    @return: 网关实例
    """
    from app.model.config import gateway_clients
    if not gateway_clients:
        reload.publish(msg={})
    gateway_client = gateway_clients.get(name)
    return gateway_client


def clear_client():
    funboost_aps_scheduler.remove_all_jobs()
    from app.model.config import discovery_clients, gateway_clients

    discovery_clients.clear()
    get_discovery_client.cache_clear()

    gateway_clients.clear()
    get_gateway_client.cache_clear()

    enginex, sqla_helper = db.get_sqla_helper()
    Jobs.create_table_if_not_exists(sqla_helper)
    Jobs.clear_all(sqla_helper)


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
            service.last_time = last_time and last_time > 0 or int(NbTime().timestamp)
        logger.info(
            f"同步服务实例, 作业: {target.get('id')}, service_name: {service.name}, 最后更新时间为: {NbTime(service.last_time).datetime_str} ,instances: {discovery_instances}")

        sqla_helper = db.get_sqla_helper()[1]
        healthcheck = target.get("healthcheck", {})
        if healthcheck:
            try:
                DiscoveryInstance({"target_id": target.get('id'), "service": service.name}).save_or_update(
                    discovery_instances, sqla_helper)
                # 拿到 discovery_instances 和 health_check 里的 unhealthy 比较，将 discovery 的下掉，保留 >= min-hosts
                instances = DiscoveryInstance(
                    {"target_id": target.get('id'), "service": service.name}).get_target_service_all_instance(
                    healthcheck.get("min-hosts", 1), sqla_helper)
                unhealthy = [d for d in instances if d.status == "unhealthy"]
                # 总节点-不健康节点>最小检查数(要保留的节点数)
                if unhealthy:
                    # 移除 discovery_instances 中 unhealthy 实例
                    unhealthy_instances = [d for d in discovery_instances if
                                           f"{d.ip}:{d.port}" in [b.instance for b in unhealthy]]
                    # 重新修改注册中心实例
                    discovery_instances = [d for d in discovery_instances if
                                           f"{d.ip}:{d.port}" not in [b.instance for b in unhealthy]]
                    if unhealthy_instances:
                        for unhealthy_instance in unhealthy_instances:
                            unhealthy_instance.change = True
                            unhealthy_instance.enabled = False
                        # 下线 unhealthy 实例
                        registration = Registration(service_name=service.name, ext_data=target.get("config", {}))
                        discovery_client.modify_registration(registration, unhealthy_instances)
            except Exception as e:
                logger.warning(f"健康检查下线实例失败, {target.get('id', None)} , {service.name}", exc_info=e)

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
        Jobs(**target).save_or_update(sqla_helper)


@boost(boost_params=FunboostCommonConfig(queue_name='queue_reload_job', qps=1, ))
def reload():
    logger.info("load config yaml")
    from app.model.config import settings, discovery_clients, gateway_clients
    clear_client()
    # key 为 name，value 为 discovery client
    if settings.config.discovery_servers:
        for name, discovery in settings.config.discovery_servers.items():
            cls = getattr(importlib.import_module(f"app.service.discovery.{discovery.type.value}"),
                          ''.join([k.title() for k in discovery.type.value.split('_')]))
            client = cls(discovery)
            discovery_clients[name] = client
    # key 为 name，value 为 gateway client
    if settings.config.gateway_servers:
        for name, gateway in settings.config.gateway_servers.items():
            cls = getattr(importlib.import_module(f"app.service.gateway.{gateway.type.value}"),
                          ''.join([k.title() for k in gateway.type.value.split('_')]))
            client = cls(gateway)
            gateway_clients[name] = client
    sqla_helper = db.get_sqla_helper()[1]
    if settings.config.targets:
        for index, target in enumerate(settings.config.targets):
            target.id = f"{index}-{target.gateway}-{target.discovery}"

            if target.enabled:
                second, minute, hour, day, month, day_of_week, next_run_time = time_parser(target.fetch_interval)
                Jobs(**target.dict()).save_or_update(sqla_helper)
                # 注册定时任务
                if next_run_time:
                    funboost_aps_scheduler.add_push_job(syncer, id=target.id, name=target.id,
                                                        next_run_time=next_run_time,
                                                        kwargs={"target": target.model_dump()}, replace_existing=True)
                else:
                    funboost_aps_scheduler.add_push_job(syncer, id=target.id, name=target.id,
                                                        trigger=CronTrigger(second=second, minute=minute, hour=hour,
                                                                            day=day, month=month,
                                                                            day_of_week=day_of_week),
                                                        kwargs={"target": target.model_dump()}, replace_existing=True)
                # 健康检查
                if target.healthcheck:
                    funboost_aps_scheduler.add_push_job(health_check, id="health-check",
                                                        name="health-check",
                                                        trigger=CronTrigger(second=second, minute=minute, hour=hour,
                                                                            day=day, month=month,
                                                                            day_of_week=day_of_week),
                                                        kwargs={"target": target.model_dump()}, replace_existing=True)


def time_parser(fetch_interval: str = ''):
    assert len(fetch_interval) > 0, "作业表达式不能为空"
    """
    值                     | 描述                                                 | 等效于
	-----                  | -----------                                         | -------------
	@yearly (or @annually) | 每年1月1日 午夜零点零分零秒执行一次                     | 0 0 0 1 1 *
	@monthly               | 每月1日 午夜零点零分零秒执行一次                        | 0 0 0 1 * *
	@weekly                | 每周日的午夜零点零分零秒执行一次                        | 0 0 0 * * 0
	@daily (or @midnight)  | 每天的午夜零点零分零秒执行一次                          | 0 0 0 * * *
	@hourly                | 每小时的零分零秒执行一次                               | 0 0 * * * *
	@reboot                | 启动时执行一次                                        | -
	@every                 | 每多久执行一次(仅支持s(秒)/m(分)/h(时),且一次只能用一种)  | */30 * * * * *  
    """
    values = fetch_interval.split()
    next_run_time = None
    if "@" in fetch_interval:
        match values[0]:
            case "@yearly" | "@annually":
                fetch_interval = "0 0 0 1 1 *"
            case "@monthly":
                fetch_interval = "0 0 0 1 * *"
            case "@weekly":
                fetch_interval = "0 0 0 * * 0"
            case "@daily" | "@midnight":
                fetch_interval = "0 0 0 * * *"
            case "@hourly":
                fetch_interval = "0 0 * * * *"
            case "@reboot":
                next_run_time = datetime.datetime.now()
            case "@every":
                matches = re.findall(r'(\d+)([a-zA-Z]+)', values[1])
                # 兼容 1h 1hour 1hours 1min 1minute 1minutes 1sec 1second 1seconds
                times = [(int(num), unit.lower()[0]) for num, unit in matches]
                assert len(times) == 1, f"fetch_interval格式错误: {fetch_interval}"
                match times[0][1]:
                    case "h":
                        fetch_interval = f"0 0 */{times[0][0]} * * *"
                    case "m":
                        fetch_interval = f"0 */{times[0][0]} * * * *"
                    case "s":
                        fetch_interval = f"*/{times[0][0]} * * * * *"
    if next_run_time:
        return None, None, None, None, None, None, next_run_time
    else:
        values = fetch_interval.split()
        # 秒 分、时、日、月、周几
        # * * * * * *
        if len(values) == 5:
            fetch_interval = f"* {fetch_interval}"
            values = fetch_interval.split()
        assert len(values) == 6, f"fetch_interval格式错误: {fetch_interval}"
        return values[0], values[1], values[2], values[3], values[4], values[5], None
