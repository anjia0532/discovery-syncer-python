"""同步核心纯逻辑：只同步**一个服务**，供订阅事件与全量对账共用。

把原来 ``syncer()`` 里的大循环拆出来，目的是让「差异计算」可离线测试、
并且事件驱动时不必再遍历全部服务、也不必拉全部网关 upstream。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from app.model.syncer_model import Instance

__all__ = ["SyncResult", "compute_diff", "is_excluded", "sync_one_service"]

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """单次单服务同步结果。"""

    service: str = ""
    changed: bool = False
    diff_count: int = 0
    skipped: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "service": self.service, "changed": self.changed,
            "diff_count": self.diff_count, "skipped": self.skipped, "error": self.error,
        }


def is_excluded(service_name: str, patterns: Sequence[str]) -> bool:
    """判断服务名是否命中 ``exclude-service`` 正则列表。

    Args:
        service_name: 服务名。
        patterns: 正则表达式列表。

    Returns:
        命中返回 ``True``。
    """
    for pattern in patterns or []:
        try:
            if re.match(pattern, service_name):
                return True
        except re.error:
            logger.warning("exclude-service 正则非法: %s", pattern)
    return False


def compute_diff(
    discovery_instances: Optional[List[Instance]],
    gateway_instances: Optional[List[Instance]],
) -> List[Instance]:
    """计算注册中心实例与网关实例的差异。

    差异条件（与原实现语义一致）：仅注册中心有 / 仅网关有 / 权重不同。
    使用 ``model_copy`` 生成新对象，避免像原实现那样就地 ``_replace`` 污染上游快照。

    Args:
        discovery_instances: 注册中心实例列表。
        gateway_instances: 网关 upstream 中的实例列表。

    Returns:
        需要变更的 ``Instance`` 列表，``change=True``，
        ``enabled`` 表示该节点在注册中心侧仍存在（网关侧应新增/保留）。
    """
    dim = {f"{item.ip}:{item.port}": item for item in (discovery_instances or [])}
    gim = {f"{item.ip}:{item.port}": item for item in (gateway_instances or [])}
    diff: List[Instance] = []
    for key in sorted(set(dim) | set(gim)):
        d = dim.get(key)
        g = gim.get(key)
        if d is None or g is None or d.weight != g.weight:
            base = d or g
            diff.append(base.model_copy(update={"change": True, "enabled": d is not None}))
    return diff


def sync_one_service(
    target: Dict[str, Any],
    service_name: str,
    discovery_client: Any,
    gateway_client: Any,
    *,
    healthcheck_hook: Optional[Callable[[Dict[str, Any], str, List[Instance]], List[Instance]]] = None,
    on_heartbeat: Optional[Callable[[str], None]] = None,
    log: Optional[Any] = None,
    prefetched_instances: Optional[List[Instance]] = None,
) -> SyncResult:
    """同步单个服务：注册中心实例 -> 网关 upstream。

    Args:
        target: target 配置字典（``model_dump()`` 结果）。
        service_name: 服务名。
        discovery_client: 注册中心客户端，需实现 ``get_service_all_instances``。
        gateway_client: 网关客户端，需实现 ``get_service_all_instances`` 与 ``sync_instances``。
        healthcheck_hook: 可选，``(target, service_name, instances) -> filtered``，
            由上层注入以便复用现有主动健康检查 + SQLite 状态逻辑。
        on_heartbeat: 可选，``(target_id) -> None``，每次同步完成（含无变更）都调用。
        log: 可选 logger。
        prefetched_instances: 全量对账时由 ``get_all_service`` 已取得的实例，
            避免重复请求。**空列表会被视为「未取得」而重新拉取**，防止
            Nacos 3.x 服务列表不返回实例时误清空网关 upstream。

    Returns:
        :class:`SyncResult`。
    """
    active_log = log or logger
    result = SyncResult(service=service_name)
    target_id = target.get("id") or ""
    if is_excluded(service_name, target.get("exclude_service", []) or []):
        result.skipped = True
        return result
    if not discovery_client or not gateway_client:
        result.skipped = True
        result.error = "discovery/gateway client 未就绪"
        return result

    try:
        if prefetched_instances:
            discovery_instances, _last_time = list(prefetched_instances), 0
        else:
            discovery_instances, _last_time = discovery_client.get_service_all_instances(
                service_name, target.get("config") or {}
            )
        if healthcheck_hook is not None:
            discovery_instances = healthcheck_hook(target, service_name, discovery_instances or [])
        gateway_instances = gateway_client.get_service_all_instances(target, service_name)
        diff = compute_diff(discovery_instances, gateway_instances)
        result.diff_count = len(diff)
        if not diff:
            active_log.info("没有变更实例,跳过更新, 作业: %s, service_name: %s", target_id, service_name)
        else:
            active_log.info("获取变更实例列表, 作业: %s, service_name: %s, instances: %s",
                            target_id, service_name, diff)
            gateway_client.sync_instances(target, service_name, diff, discovery_instances)
            result.changed = True
        # 只有成功（含无变更）才刷新心跳。失败时故意不刷新，
        # /health 才能通过 maximum-interval-sec 感知同步故障；
        # 这也是去掉 funboost 死信队列后保留可观测性的关键。
        if on_heartbeat:
            try:
                on_heartbeat(target_id)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 - 单服务失败不能拖垮其它服务
        result.error = f"{type(exc).__name__}: {exc}"
        active_log.warning("同步服务失败 作业=%s service=%s err=%s", target_id, service_name, exc)
    return result
