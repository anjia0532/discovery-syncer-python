import base64
import re
from typing import Annotated, Optional

from fastapi import APIRouter
from fastapi import Response
from fastapi.params import Path, Body, Query

from . import RESP_OK
from app.model.syncer_model import Registration, RegistrationType, RegistrationStatus
from app.service.discovery.discovery import Discovery
from app.service.gateway.gateway import Gateway
from core.lib.logger import for_handler

router = APIRouter()

logger = for_handler(__name__)


@router.put("/discovery/{discovery_name}", summary="主动下线上线注册中心的服务",
            description="主动下线上线注册中心的服务<br/>配合CI/CD发版业务用")
def discovery(discovery_name: Annotated[str, Path(title="discovery_name", description="注册中心名称")],
              registration: Annotated[Registration, Body(title="Registration", description="入参")],
              alive_num: Annotated[int, Query(title="alive_num", description="最少存活实例数")] = 1):
    """
    主动下线上线注册中心的服务,配合CI/CD发版业务用
    @param discovery_name: 注册中心名称
    @param alive_num: 最少存活实例数，默认1，如果小于等于0，则不限制，如果大于0，则执行上下线操作时需要满足最少存活实例数，否则会抛异常
    @param registration: 服务上下线信息
    @rtype: str 成功返回 OK
    @return: 成功返回 OK
    """
    try:
        from app.model.config import discovery_clients
        discovery_client: Discovery = discovery_clients.get(discovery_name)
        if not discovery_client:
            return Response(status_code=404, content=f"没有获取到注册中心实例{discovery_name}")
        discovery_instances, last_time = discovery_client.get_service_all_instances(registration.service_name,
                                                                                    registration.ext_data,
                                                                                    enabled_only=False)
        instances = []
        for instance in discovery_instances:
            val = ""
            if registration.type == RegistrationType.METADATA:
                val = instance.metadata.get(registration.metadata_key, "")
                if len(val) == 0:
                    if registration.other_status != RegistrationStatus.ORIGIN:
                        instance.enabled = registration.other_status == RegistrationStatus.UP
                        instance.change = True
            elif registration.type == RegistrationType.IP:
                val = instance.ip
            if re.match(registration.regexp_str or '', val):
                instance.enabled = registration.status == RegistrationStatus.UP
                instance.change = True
            else:
                if registration.other_status != RegistrationStatus.ORIGIN:
                    instance.enabled = registration.other_status == RegistrationStatus.UP
                    instance.change = True
            if instance.change:
                instances.append(instance)
        # 限制最少存活实例数
        if alive_num > 0:
            down_hosts = [f"{instance.ip}:{instance.port}" for instance in discovery_instances if
                          instance.change and not instance.enabled]
            alive_hosts = [f"{instance.ip}:{instance.port}" for instance in discovery_instances if instance.enabled]
            if len(alive_hosts) < alive_num:
                raise Exception(
                    f"最少存活实例数{alive_num}不满足，总实例数(含之前已下线数量){len(discovery_instances)}，要下线实例数{len(down_hosts)}，剩余在线实例数{len(alive_hosts)}")
        discovery_client.modify_registration(registration, instances=instances)
    except Exception as e:
        logger.error(f"主动下线上线注册中心的服务失败,discovery_name {discovery_name},registration {registration}",
                     exc_info=e)
        return Response(status_code=500, content=f"{e.args}")
    return Response(status_code=200, content=RESP_OK)


@router.get("/gateway-api-to-file/{gateway_name}")
def gateway_to_file(gateway_name: str = Path(title="gateway_name", description="网关中心名称"),
                    file_name: Optional[str] = Query(default=None, title="file_name", description="文件名称")):
    """
    读取网关admin api转换成文件用于备份或者db-less模式
    @rtype: str
    @param gateway_name: 网关名称
    @param file_name: 文件名称
    @return: 成功返回db-less配置文件
    """
    try:
        from app.model.config import gateway_clients
        gateway_client: Gateway = gateway_clients.get(gateway_name)
        if not gateway_client:
            return Response(status_code=404, content=f"没有获取到网关实例{gateway_name}")
        content, file = gateway_client.fetch_admin_api_to_file(file_name)
        return Response(status_code=200, content=content, headers={"syncer-file-location": f"{file}"})
    except Exception as e:
        logger.error(
            f"读取网关admin api转换成文件用于备份或者db-less模式失败,gateway_name :{gateway_name},file_name:{file_name}",
            exc_info=e)
        return Response(status_code=500,
                        headers={"syncer-err-msg": base64.b64encode(f"{e.args}".encode("utf-8")).decode("utf-8")})


@router.post("/migrate/{origin_gateway_name}/to/{target_gateway_name}")
async def migrate_gateway(
        origin_gateway_name: Annotated[str, Path(title="origin_gateway_name", description="数据来源网关中心名称")],
        target_gateway_name: Annotated[str, Path(title="target_gateway_name", description="数据迁入目标网关中心名称")]):
    """
    将网关数据迁移(目前仅支持apisix)
    @rtype: str
    @param origin_gateway_name: 数据来源网关中心名称
    @param target_gateway_name: 数据迁入目标网关中心名称
    @return: 成功返回 OK
    """
    try:
        from app.model.config import gateway_clients

        origin_gateway_client: Gateway = gateway_clients.get(origin_gateway_name)
        if not origin_gateway_client:
            return Response(status_code=404, content=f"没有获取到数据来源网关实例{origin_gateway_name}")

        target_gateway_client: Gateway = gateway_clients.get(target_gateway_name)
        if not target_gateway_client:
            return Response(status_code=404, content=f"没有获取到数据迁入网关实例{target_gateway_name}")

        await origin_gateway_client.migrate_to(target_gateway_client)
    except Exception as e:
        logger.error(
            f"将网关数据迁移失败,origin_gateway_name :{origin_gateway_name},target_gateway_name:{target_gateway_name}",
            exc_info=e)
        return Response(status_code=500, content=f"{e.args}")
    return Response(status_code=200, content=RESP_OK)


@router.put("/restore/{target_gateway_name}")
async def restore_gateway(
        target_gateway_name: Annotated[str, Path(title="target_gateway_name", description="数据迁入目标网关中心名称")],
        body: Annotated[str, Body(title="body", description="待还原配置文件数据")]):
    """
    通过文件还原网关数据(目前仅支持apisix)
    @param body: 待还原配置文件数据
    @param target_gateway_name: 数据迁入目标网关中心名称
    @return: 成功返回 OK
    """
    try:
        from app.model.config import gateway_clients

        target_gateway_client: Gateway = gateway_clients.get(target_gateway_name)
        if not target_gateway_client:
            return Response(status_code=404, content=f"没有获取到待还原网关实例{target_gateway_name}")
        await target_gateway_client.restore_gateway(body)
    except Exception as e:
        logger.error(
            f"通过文件还原网关数据,target_gateway_name :{target_gateway_name},body:{body}",
            exc_info=e)
        return Response(status_code=500, content=f"{e.args}")
    return Response(status_code=200, content=RESP_OK)
