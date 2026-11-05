"""订阅编排层：按 target 装配 Provider、管理生命周期、自动降级、暴露健康状态。

Manager 不依赖 app.compat / funboost / FastAPI，能力信息由调用方填进
:class:`TargetSpec`；事件投递通过注入的 ``publisher`` 回调桥接到消息队列。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .core import ChangeEvent, EventBus, ProviderKind, SubscribeMode, SubscriptionProvider
from .providers import (
    NacosGrpcSubscription,
    NacosUdpSubscription,
    SnapshotPollingSubscription,
    WebhookSubscription,
)

__all__ = ["TargetSpec", "TargetRuntime", "SubscriptionManager"]


@dataclass
class TargetSpec:
    """单个同步作业的订阅装配参数。

    Attributes:
        target_id: 作业 ID。
        discovery: 注册中心名。
        mode: 订阅策略。
        push_transport: 能力矩阵给出的首选推送通道（udp/grpc/webhook/poll/none）。
        major / label / evidence: 版本信息，用于 ``/-/versions`` 与日志。
        list_services: 拉取全部服务（UDP 续约 / 发现新服务用）。
        send_subscribe: 发送 UDP 订阅请求，``(name, group, namespace, udp_port)``。
        snapshot_provider: 拉取上游快照（增量轮询用）。
        grpc_stream: 可选的真实 gRPC 事件流。
        grpc_probe / grpc_host / grpc_port: gRPC 探测。
        refresh_interval_sec: UDP 订阅续约周期。
        poll_interval_sec: 增量轮询周期。
        push_timeout_sec: 判定推送失联的秒数。
        udp_host / udp_port: UDP 监听地址。
        webhook_enabled: 是否附带入站 webhook receiver。
        fallback_enabled: 推送降级时是否自动叠加增量轮询。
    """

    target_id: str
    discovery: str
    mode: SubscribeMode = SubscribeMode.AUTO
    push_transport: str = "poll"
    major: int = 1
    label: str = ""
    evidence: str = ""
    list_services: Callable[[], List[Any]] = lambda: []
    send_subscribe: Optional[Callable[[str, str, str, int], None]] = None
    snapshot_provider: Callable[[], List[Any]] = lambda: []
    grpc_stream: Optional[Callable[[], Any]] = None
    grpc_probe: Optional[Callable[[str, int], bool]] = None
    grpc_host: str = ""
    grpc_port: int = 0
    refresh_interval_sec: float = 30.0
    poll_interval_sec: float = 10.0
    push_timeout_sec: float = 0.0
    udp_host: str = "0.0.0.0"
    udp_port: int = 0
    webhook_enabled: bool = True
    fallback_enabled: bool = True


@dataclass
class TargetRuntime:
    """运行时状态。"""

    spec: TargetSpec
    providers: List[SubscriptionProvider] = field(default_factory=list)
    fallback_added: bool = False
    last_heartbeat: float = field(default_factory=time.time)
    event_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        providers = []
        for provider in self.providers:
            h = provider.health()
            providers.append({"kind": provider.kind.value, **h.to_dict()})
        return {
            "target_id": self.spec.target_id,
            "discovery": self.spec.discovery,
            "mode": self.spec.mode.value,
            "version": self.spec.label,
            "version_evidence": self.spec.evidence,
            "push_transport": self.spec.push_transport,
            "fallback_added": self.fallback_added,
            "last_heartbeat": self.last_heartbeat,
            "event_count": self.event_count,
            "degraded": any(p["degraded"] for p in providers),
            "alive": any(p["alive"] for p in providers),
            "providers": providers,
        }


class SubscriptionManager:
    """订阅生命周期编排。

    Args:
        publisher: ``(target_id, service_name, event) -> None``，由本地任务总线桥接。
        debounce_ms: 事件去抖窗口。
        supervise_interval: 降级巡检周期秒数。
        logger: 可选 logger。
        clock: 时间函数，测试可注入。
    """

    def __init__(
        self,
        *,
        publisher: Optional[Callable[[str, str, ChangeEvent], None]] = None,
        debounce_ms: int = 500,
        supervise_interval: float = 5.0,
        logger: Optional[Any] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.log = logger or logging.getLogger("subscription.Manager")
        self._clock = clock
        self._publisher = publisher
        self._debounce_ms = debounce_ms
        self._bus = EventBus(debounce_ms=debounce_ms, publisher=self._publish)
        self._lock = threading.RLock()
        self._runtimes: Dict[str, TargetRuntime] = {}
        self._by_discovery: Dict[str, List[str]] = {}
        self._supervise_interval = supervise_interval
        self._supervise_stop = threading.Event()
        self._supervise_thread: Optional[threading.Thread] = None
        self._bus.start()

    # -- 装配 ---------------------------------------------------------------
    def rebuild(self, specs: List[TargetSpec]) -> Dict[str, Any]:
        """全量重建订阅（``/-/reload`` 时调用），返回装配摘要。

        先停掉所有旧 Provider 再启动新的，保证重复 reload 不会残留线程/端口。

        Args:
            specs: 所有 enabled target 的装配参数。

        Returns:
            ``{target_id: status_dict}``。
        """
        self.stop_all()
        with self._lock:
            for spec in specs:
                runtime = TargetRuntime(spec=spec, providers=self._build_providers(spec))
                self._runtimes[spec.target_id] = runtime
                self._by_discovery.setdefault(spec.discovery, []).append(spec.target_id)
        for runtime in list(self._runtimes.values()):
            for provider in runtime.providers:
                try:
                    provider.start()
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("Provider 启动失败 target=%s kind=%s err=%s",
                                     runtime.spec.target_id, provider.kind.value, exc)
        self._start_supervisor()
        return {tid: rt.to_dict() for tid, rt in self._runtimes.items()}

    def _build_providers(self, spec: TargetSpec) -> List[SubscriptionProvider]:
        """按 mode + 版本能力生成 Provider 列表。

        * ``OFF``   → 不装配（回退旧定时轮询）。
        * ``WEBHOOK`` → 仅入站回调。
        * ``POLL``  → 仅快照 diff 增量轮询。
        * ``PUSH`` / ``AUTO`` → 版本对应的原生推送；AUTO 额外叠加 webhook，
          推送失联时由 :meth:`supervise_once` 动态追加轮询兜底。
        """
        mode = spec.mode
        if mode == SubscribeMode.OFF:
            return []

        emit = lambda ev: self.emit(spec.target_id, ev)  # noqa: E731
        common = dict(target_id=spec.target_id, discovery=spec.discovery, emit=emit, logger=self.log)
        if mode == SubscribeMode.WEBHOOK:
            return [WebhookSubscription(**common)]
        if mode == SubscribeMode.POLL:
            return [self._make_polling(spec, emit)]

        transport = spec.push_transport
        if mode == SubscribeMode.PUSH and transport not in ("udp", "grpc"):
            transport = "udp" if spec.major == 1 else "grpc"

        providers: List[SubscriptionProvider] = []
        if transport == "udp" and spec.send_subscribe is not None:
            providers.append(NacosUdpSubscription(
                **common,
                list_services=spec.list_services,
                send_subscribe=spec.send_subscribe,
                refresh_interval_sec=spec.refresh_interval_sec,
                push_timeout_sec=spec.push_timeout_sec,
                udp_host=spec.udp_host,
                udp_port=spec.udp_port,
            ))
        elif transport == "grpc":
            providers.append(NacosGrpcSubscription(
                **common,
                grpc_stream=spec.grpc_stream,
                grpc_probe=spec.grpc_probe,
                grpc_host=spec.grpc_host,
                grpc_port=spec.grpc_port,
            ))
        # 无 push 能力（Eureka / 网关侧）时，至少保证有增量轮询。
        if not providers:
            providers.append(self._make_polling(spec, emit))
        if spec.webhook_enabled and not any(p.kind == ProviderKind.WEBHOOK for p in providers):
            providers.append(WebhookSubscription(**common))
        return providers

    def _make_polling(self, spec: TargetSpec, emit: Callable[[ChangeEvent], None]) -> SnapshotPollingSubscription:
        return SnapshotPollingSubscription(
            target_id=spec.target_id, discovery=spec.discovery, emit=emit,
            snapshot_provider=spec.snapshot_provider,
            interval_sec=spec.poll_interval_sec,
            emit_initial=True,
            logger=self.log,
        )

    # -- 事件 ---------------------------------------------------------------
    def emit(self, target_id: str, event: ChangeEvent) -> None:
        """Provider 事件入口：计数 + 刷新心跳 + 投入去抖总线。"""
        with self._lock:
            runtime = self._runtimes.get(target_id)
            if runtime:
                runtime.event_count += 1
                runtime.last_heartbeat = self._clock()
        self._bus.emit(target_id, event)

    def _publish(self, target_id: str, service_name: str, event: ChangeEvent) -> None:
        if self._publisher is None:
            return
        self._publisher(target_id, service_name, event)

    def handle_webhook(self, discovery: str, payload: Dict[str, Any]) -> int:
        """把入站 webhook 分发给绑定该注册中心的所有 target，返回事件数。"""
        total = 0
        with self._lock:
            target_ids = list(self._by_discovery.get(discovery, []))
            runtimes = [self._runtimes[t] for t in target_ids if t in self._runtimes]
        for runtime in runtimes:
            for provider in runtime.providers:
                if provider.kind == ProviderKind.WEBHOOK:
                    total += provider.notify(payload)
        return total

    def heartbeat(self, target_id: str) -> None:
        """全量对账/单服务同步成功后刷新心跳（供 /health 判定失联）。"""
        with self._lock:
            runtime = self._runtimes.get(target_id)
            if runtime:
                runtime.last_heartbeat = self._clock()

    # -- 巡检与降级 ---------------------------------------------------------
    def _start_supervisor(self) -> None:
        if self._supervise_thread and self._supervise_thread.is_alive():
            return
        self._supervise_stop.clear()
        self._supervise_thread = threading.Thread(
            target=self._supervise_loop, name="subscription-supervisor", daemon=True
        )
        self._supervise_thread.start()

    def _supervise_loop(self) -> None:
        while not self._supervise_stop.wait(self._supervise_interval):
            self.supervise_once()

    def supervise_once(self) -> List[str]:
        """检查推送是否失联，必要时叠加快照轮询兜底，返回发生降级的 target_id 列表。"""
        degraded_now: List[str] = []
        with self._lock:
            runtimes = list(self._runtimes.items())
        for target_id, runtime in runtimes:
            push_providers = [
                p for p in runtime.providers
                if p.kind in (ProviderKind.NACOS_UDP, ProviderKind.NACOS_GRPC)
            ]
            needs_fallback = bool(push_providers) and all(p.health().degraded for p in push_providers)
            if not needs_fallback or runtime.fallback_added or not runtime.spec.fallback_enabled:
                continue
            spec = runtime.spec
            emit = lambda ev, tid=target_id: self.emit(tid, ev)  # noqa: E731
            fallback = self._make_polling(spec, emit)
            with self._lock:
                runtime.providers.append(fallback)
                runtime.fallback_added = True
            try:
                fallback.start()
            except Exception as exc:  # noqa: BLE001
                self.log.warning("降级兜底启动失败 target=%s err=%s", target_id, exc)
            self.log.warning("推送通道不可用，已叠加快照 diff 兜底 target=%s version=%s",
                             target_id, spec.label)
            degraded_now.append(target_id)
        return degraded_now

    # -- 状态与生命周期 -----------------------------------------------------
    def status(self, target_id: str) -> Dict[str, Any]:
        """单个作业订阅状态；未订阅返回空 dict。"""
        with self._lock:
            runtime = self._runtimes.get(target_id)
            return runtime.to_dict() if runtime else {}

    def all_status(self) -> Dict[str, Dict[str, Any]]:
        """全部作业订阅状态。"""
        with self._lock:
            return {tid: rt.to_dict() for tid, rt in self._runtimes.items()}

    def last_heartbeat(self, target_id: str) -> Optional[float]:
        """最近心跳时间，供 /health 判定失联；无订阅返回 None。"""
        with self._lock:
            runtime = self._runtimes.get(target_id)
            return runtime.last_heartbeat if runtime else None

    def stop_all(self) -> None:
        """停止全部 Provider 与巡检线程（不清空 publisher，便于再次 rebuild）。"""
        self._supervise_stop.set()
        if self._supervise_thread and self._supervise_thread.is_alive():
            self._supervise_thread.join(timeout=2.0)
        self._supervise_thread = None
        self._bus.stop()
        with self._lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
            self._by_discovery.clear()
        for runtime in runtimes:
            for provider in runtime.providers:
                try:
                    provider.stop()
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("Provider 停止失败 target=%s err=%s", runtime.spec.target_id, exc)
        self._bus.start()
