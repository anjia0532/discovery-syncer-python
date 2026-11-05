"""订阅相关运维接口。

* ``POST /-/notify``            入站变更回调（任意版本可叠加）。
* ``GET  /-/subscriptions``     各作业订阅状态与 Provider 健康。
* ``GET  /-/versions``          注册中心/网关版本探测结果与能力矩阵。

所有接口沿用既有 ``SYNCER-API-KEY`` 中间件鉴权。
"""

from typing import Annotated, Any, Dict, Optional

from fastapi import APIRouter, Body, Header, Query
from starlette.responses import JSONResponse, Response

from core.lib.logger import for_handler

router = APIRouter()
logger = for_handler(__name__)


def _notify_tokens(discovery: str) -> list:
    """收集绑定该注册中心的 target 上配置的 webhook 令牌（空串表示不校验）。"""
    from app.model.config import settings

    tokens = []
    for target in (settings.config.targets or []):
        if target.discovery == discovery and target.subscribe and target.subscribe.notify_token:
            tokens.append(target.subscribe.notify_token)
    return tokens


@router.post("/-/notify", summary="上游变更回调", description="接收上游注册中心/CI 推送的服务变更事件")
@router.post("/-/notify/{discovery_name}", summary="上游变更回调", description="接收指定注册中心的服务变更事件")
def notify(
        discovery_name: Optional[str] = None,
        payload: Annotated[Dict[str, Any], Body(title="payload", description="变更事件")] = None,
        discovery: Annotated[Optional[str], Query(title="discovery", description="注册中心名")] = None,
        syncer_notify_token: Annotated[Optional[str], Header(alias="SYNCER-NOTIFY-TOKEN")] = None,
) -> Response:
    """把一次变更通知分发给订阅了对应注册中心的作业。

    载荷示例::

        {"serviceName": "mall-order", "groupName": "DEFAULT_GROUP", "namespaceId": "mall"}
        {"services": ["mall-order", "mall-user"], "namespaceId": "public"}

    Args:
        discovery_name: 路径参数形式的注册中心名。
        payload: 变更事件 JSON。
        discovery: query 参数形式的注册中心名。
        syncer_notify_token: 可选令牌，与 ``subscribe.notify-token`` 比对。

    Returns:
        200 + 分发事件数；未匹配到注册中心返回 404。
    """
    from app.tasks.task_syncer import get_manager

    name = discovery_name or discovery or (payload or {}).get("discovery")
    if not name:
        return JSONResponse(status_code=400, content={"error": "缺少 discovery 参数"})
    tokens = _notify_tokens(name)
    if tokens and syncer_notify_token not in tokens:
        return JSONResponse(status_code=401, content={"error": "notify-token 不匹配"})
    count = get_manager().handle_webhook(name, payload or {})
    logger.info("入站变更回调 discovery=%s payload=%s -> %s events", name, payload, count)
    return JSONResponse(status_code=200, content={"accepted": count, "discovery": name})


@router.get("/-/subscriptions", summary="订阅状态", description="查看各同步作业的订阅通道与健康状态")
def subscriptions() -> Response:
    """返回每个 target 的订阅策略、版本、Provider 健康与事件计数。

    追加 ``_runtime`` 字段暴露本地任务总线统计与调度作业列表，
    替代原 funboost 死信/消费者心跳提供的可观测性。
    """
    from app.runtime import get_elector, get_scheduler, get_task_bus
    from app.tasks.task_syncer import get_manager

    status = get_manager().all_status()
    elector = get_elector()
    status["_runtime"] = {
        "taskbus": get_task_bus().stats,
        "scheduler_jobs": get_scheduler().job_ids,
        "deployment": "cluster" if elector is not None else "single-replica",
        "cluster": elector.state.to_dict() if elector is not None else {"enabled": False},
    }
    return JSONResponse(status_code=200, content=status)


@router.get("/-/cluster", summary="集群状态", description="查看本副本的角色、租约与主备切换计数")
def cluster_status() -> Response:
    """返回本副本的集群状态。

    未启用集群时返回 ``{"enabled": false, "role": "standalone"}``。
    """
    from app.runtime import get_elector

    elector = get_elector()
    if elector is None:
        return JSONResponse(status_code=200, content={
            "enabled": False, "role": "standalone",
            "note": "未启用集群模式（单副本），不依赖 Redis",
        })
    return JSONResponse(status_code=200, content=elector.state.to_dict())


@router.get("/-/versions", summary="版本适配", description="查看各注册中心/网关的版本探测结果与能力")
def versions() -> Response:
    """返回版本能力矩阵命中情况，便于排障多版本适配。"""
    from app.model.config import discovery_clients, gateway_clients
    from app.tasks.task_syncer import _ensure_clients

    # 遍历需要完整字典，先确保已初始化（避免 reload 窗口期返回空结果）
    _ensure_clients()

    def describe(client: Any) -> Dict[str, Any]:
        cap = getattr(client, "capability", None)
        if cap is None and hasattr(client, "detect_version"):
            cap = client.detect_version()
        if cap is None:
            return {"version": "unknown", "evidence": getattr(client, "version_evidence", "")}
        return {
            "version": cap.label,
            "major": cap.major,
            "api_prefix": cap.api_prefix,
            "push": cap.push.value,
            "list_shape": cap.list_shape.value,
            "target_write": cap.target_write,
            "grpc_offset": cap.grpc_offset,
            "default_namespace": cap.default_namespace,
            "notes": cap.notes,
            "evidence": getattr(client, "version_evidence", ""),
        }

    return JSONResponse(status_code=200, content={
        "discovery": {name: describe(c) for name, c in discovery_clients.items()},
        "gateway": {name: describe(c) for name, c in gateway_clients.items()},
    })
