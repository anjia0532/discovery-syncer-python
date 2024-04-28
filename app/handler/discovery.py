import re
from typing import Annotated

from fastapi import APIRouter
from fastapi import Response
from fastapi.params import Path, Body

from app.model.syncer_model import Registration, RegistrationType, RegistrationStatus
from app.service.discovery.discovery import Discovery
from app.service.gateway.gateway import Gateway
from core.lib.logger import for_handler

router = APIRouter()

logger = for_handler(__name__)


@router.put("/discovery/{discovery_name}", summary="主动下线上线注册中心的服务",
            description="主动下线上线注册中心的服务<br/>配合CI/CD发版业务用")
def discovery(discovery_name: Annotated[str, Path(title="discovery_name", description="注册中心名称")],
              registration: Annotated[Registration, Body(title="Registration", description="入参")]):
    """
    主动下线上线注册中心的服务,配合CI/CD发版业务用
    @param registration: 服务上下线信息
    @param discovery_name: 注册中心名称
    @return: 结果
    """
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
                continue
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
    discovery_client.modify_registration(registration, instances=instances)
    return Response(status_code=200, content="OK")


@router.get("/gateway-api-to-file/{gateway_name}")
def gateway_to_file(gateway_name: Annotated[str, Path(title="gateway_name", description="网关中心名称")]):
    """
    读取网关admin api转换成文件用于备份或者db-less模式
    @param gateway_name: 网关名称
    @return: 结果
    """
    try:
        from app.model.config import gateway_clients
        gateway_client: Gateway = gateway_clients.get(gateway_name)
        if not gateway_client:
            return Response(status_code=404, content=f"没有获取到网关实例{gateway_name}")
        content, file = gateway_client.fetch_admin_api_to_file()
        return Response(status_code=200, content=content, headers={"syncer-file-location": f"{file}"})
    except Exception as e:
        return Response(status_code=500, headers={"syncer-err-msg": f"{e.args[0]}"})


@router.post("/migrate/{origin_gateway_name}/to/{target_gateway_name}")
async def migrate_gateway(
        origin_gateway_name: Annotated[str, Path(title="origin_gateway_name", description="数据来源网关中心名称")],
        target_gateway_name: Annotated[str, Path(title="target_gateway_name", description="数据迁入目标网关中心名称")]):
    """
    将网关数据迁移(目前仅支持apisix)
    @param origin_gateway_name: 数据来源网关中心名称
    @param target_gateway_name: 数据迁入目标网关中心名称
    @return:
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
        return Response(status_code=500, content=f"{e.args[0]}")
    return Response(status_code=200, content="OK")


@router.put("/restore/{target_gateway_name}")
async def restore_gateway(
        target_gateway_name: Annotated[str, Path(title="target_gateway_name", description="数据迁入目标网关中心名称")],
        body: Annotated[str, Body(title="body", description="待还原配置文件数据")]):
    """
    通过文件还原网关数据(目前仅支持apisix)
    @param body: 待还原配置文件数据
    @param target_gateway_name: 数据迁入目标网关中心名称
    @return:
    """
    try:
        from app.model.config import gateway_clients

        target_gateway_client: Gateway = gateway_clients.get(target_gateway_name)
        if not target_gateway_client:
            return Response(status_code=404, content=f"没有获取到待还原网关实例{target_gateway_name}")
        await target_gateway_client.restore_gateway(body)
    except Exception as e:
        return Response(status_code=500, content=f"{e.args[0]}")
    return Response(status_code=200, content="OK")
