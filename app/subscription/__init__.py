"""订阅层：把「定时全量轮询取差异」改为「上游变更事件驱动同步」。"""

from .core import ChangeEvent, EventBus, ProviderHealth, ProviderKind, SubscribeMode, SubscriptionProvider
from .manager import SubscriptionManager, TargetRuntime, TargetSpec
from .providers import (
    NacosGrpcSubscription,
    NacosUdpSubscription,
    SnapshotPollingSubscription,
    WebhookSubscription,
    fingerprint_instances,
    parse_nacos_push,
)

__all__ = [
    "ChangeEvent", "EventBus", "ProviderHealth", "ProviderKind", "SubscribeMode", "SubscriptionProvider",
    "SubscriptionManager", "TargetRuntime", "TargetSpec",
    "NacosGrpcSubscription", "NacosUdpSubscription", "SnapshotPollingSubscription",
    "WebhookSubscription", "fingerprint_instances", "parse_nacos_push",
]
