"""订阅层核心：事件模型、去抖总线、Provider 抽象。

本模块刻意不依赖 funboost / redis / nb_log，只使用标准库，
因此可以在单元测试里用 ``importlib.util.spec_from_file_location`` 独立加载。
与外部（消息队列、注册中心客户端）的交互全部通过**注入的回调**完成。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = [
    "SubscribeMode", "ProviderKind", "ChangeEvent", "ProviderHealth",
    "SubscriptionProvider", "EventBus",
]


class SubscribeMode(str, Enum):
    """订阅策略（config.yaml 里 ``subscribe.mode`` 的可选值）。"""

    AUTO = "auto"        # 按上游版本能力自动选择，失败自动降级
    PUSH = "push"        # 强制原生推送（Nacos 1.x UDP / 2.x+ gRPC）
    WEBHOOK = "webhook"  # 仅入站 HTTP 回调
    POLL = "poll"        # 强制快照 diff 增量轮询
    OFF = "off"          # 关闭订阅，完全回退到旧版定时全量轮询（灰度对比用）


class ProviderKind(str, Enum):
    """具体 Provider 类型。"""

    NACOS_UDP = "nacos-udp"
    NACOS_GRPC = "nacos-grpc"
    POLLING = "polling"
    WEBHOOK = "webhook"


@dataclass(frozen=True)
class ChangeEvent:
    """一次上游变更事件。

    Attributes:
        service_name: 发生变更的服务名。
        discovery: 注册中心名称（config.yaml 的 key）。
        group: Nacos groupName。
        namespace: Nacos namespaceId。
        source: 事件来源（udp / grpc / polling / webhook / subscribe）。
        ts: 事件产生时间（Unix 秒）。
    """

    service_name: str
    discovery: str = ""
    group: str = ""
    namespace: str = ""
    source: str = ""
    ts: float = field(default_factory=time.time)

    @property
    def dedupe_key(self) -> Tuple[str, str, str, str]:
        """用于事件合并的键。"""
        return (self.discovery, self.namespace, self.group, self.service_name)


@dataclass
class ProviderHealth:
    """Provider 运行健康快照。

    Attributes:
        alive: 线程/连接是否存活。
        degraded: 是否因推送不通而降级（Manager 据此追加兜底 Provider）。
        reason: 人类可读原因。
        last_push_at: 最近一次收到**真实推送**的时间。
        last_heartbeat_at: 最近一次心跳（订阅续约/轮询/对账均刷新）。
        events: 累计产出事件数。
    """

    alive: bool = False
    degraded: bool = False
    reason: str = ""
    last_push_at: Optional[float] = None
    last_heartbeat_at: float = field(default_factory=time.time)
    events: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alive": self.alive,
            "degraded": self.degraded,
            "reason": self.reason,
            "last_push_at": self.last_push_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "events": self.events,
        }


class SubscriptionProvider(ABC):
    """订阅提供者抽象基类。

    所有 Provider 通过构造时注入的 ``emit`` 回调把事件送进 :class:`EventBus`，
    从而与消息队列、FastAPI 生命周期解耦。

    Args:
        target_id: 所属同步作业 ID。
        discovery: 注册中心名称。
        emit: ``emit(ChangeEvent) -> None`` 回调。
        logger: 可选 logger，默认标准库 logging。
    """

    kind: ProviderKind = ProviderKind.POLLING

    def __init__(
        self,
        *,
        target_id: str,
        discovery: str,
        emit: Callable[[ChangeEvent], None],
        logger: Optional[Any] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.target_id = target_id
        self.discovery = discovery
        self._emit = emit
        self.log = logger or logging.getLogger(f"subscription.{self.__class__.__name__}")
        self._clock = clock
        self._stop_event = threading.Event()
        self._health = ProviderHealth(last_heartbeat_at=clock())

    # -- 生命周期 -----------------------------------------------------------
    @abstractmethod
    def start(self) -> None:
        """启动订阅（幂等）。"""

    @abstractmethod
    def stop(self) -> None:
        """停止订阅并释放资源（幂等）。"""

    # -- 状态 ---------------------------------------------------------------
    def health(self) -> ProviderHealth:
        """返回当前健康快照（拷贝，避免外部修改内部状态）。"""
        return ProviderHealth(**self._health.to_dict())

    def heartbeat(self) -> None:
        """刷新心跳：任何一次成功的续约/轮询/对账都应调用。"""
        self._health.last_heartbeat_at = self._clock()
        self._health.alive = True

    def _record_push(self, event: ChangeEvent) -> None:
        """记录一次真实推送并转发事件。"""
        now = self._clock()
        self._health.last_push_at = now
        self._health.last_heartbeat_at = now
        self._health.alive = True
        self._health.events += 1
        self._emit(event)

    def _record_event(self, event: ChangeEvent) -> None:
        """记录一次非推送事件（轮询/对账产出）并转发。"""
        self._health.last_heartbeat_at = self._clock()
        self._health.alive = True
        self._health.events += 1
        self._emit(event)

    def notify(self, payload: Dict[str, Any]) -> int:  # noqa: B027 - 默认无操作，Webhook 覆写
        """处理入站 webhook 载荷，返回产出的事件数。默认不支持。"""
        return 0

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()


Publisher = Callable[[str, str, ChangeEvent], None]


class EventBus:
    """事件去抖合并总线。

    上游在实例上下线时往往连续推送同一个服务的多条消息，若逐条触发同步会造成
    网关被反复 PATCH/PUT。这里按 ``(target_id, service_name)`` 在 ``debounce_ms``
    窗口内合并，窗口内只保留**最后一条**事件，到点后统一投递给 publisher。

    Args:
        debounce_ms: 去抖窗口毫秒数。
        publisher: ``publisher(target_id, service_name, event)`` 回调。
        tick: 线程轮询间隔秒数。
        clock: 单调时钟，测试可注入。
    """

    def __init__(
        self,
        debounce_ms: int = 500,
        publisher: Optional[Publisher] = None,
        tick: float = 0.02,
        clock: Callable[[], float] = time.monotonic,
        max_queue: int = 10000,
    ) -> None:
        """初始化事件总线。

        Args:
            debounce_ms: 同一服务事件的合并窗口（毫秒）。
            publisher: 事件出队后的投递函数。
            tick: 消费线程轮询间隔（秒）。
            clock: 单调时钟（测试可注入）。
            max_queue: 入队队列上限。上游突发推送时若消费不及，
                超过上限的事件按 ``dropped`` 丢弃——同步本身有 5m 全量
                对账兜底，宁丢事件也不让内存无界增长。
        """
        self._debounce = max(0, debounce_ms) / 1000.0
        self._publisher: Publisher = publisher or (lambda *_args, **_kwargs: None)
        self._tick = tick
        self._clock = clock
        self._queue: "queue.Queue[Tuple[str, ChangeEvent]]" = queue.Queue(maxsize=max_queue)
        self._pending: Dict[Tuple[str, str], Tuple[float, str, ChangeEvent]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._published = 0
        self._dropped = 0
        self._stats_lock = threading.Lock()
        self._log = logging.getLogger("subscription.EventBus")

    def start(self) -> None:
        """启动后台消费线程（幂等）。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="subscription-event-bus", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止后台线程；未到期的事件直接丢弃，避免停机瞬间触发下游同步。"""
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        with self._lock:
            self._pending.clear()

    def emit(self, target_id: str, event: ChangeEvent) -> None:
        """投递一条事件（非阻塞）。

        队列满时丢弃并计入 ``dropped``，绝不阻塞上游推送线程。
        丢弃是安全的：同步有 5 分钟全量对账兜底，不会永久丢失变更。
        """
        if not event.service_name:
            return
        try:
            self._queue.put_nowait((target_id, event))
        except queue.Full:
            with self._stats_lock:
                self._dropped += 1
                dropped = self._dropped
            if dropped % 100 == 1:
                self._log.warning(
                    "事件队列已满(maxsize=%s)，丢弃事件 target=%s service=%s 累计丢弃=%s",
                    self._queue.maxsize, target_id, event.service_name, dropped,
                )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                target_id, event = self._queue.get(timeout=self._tick)
            except queue.Empty:
                target_id, event = None, None  # type: ignore[assignment]
            if target_id is not None and event is not None:
                key = (target_id, event.service_name)
                with self._lock:
                    self._pending[key] = (self._clock() + self._debounce, target_id, event)
            self._flush_due()

    def _flush_due(self) -> None:
        now = self._clock()
        due: List[Tuple[str, str, ChangeEvent]] = []
        with self._lock:
            for key, (deadline, target_id, event) in list(self._pending.items()):
                if deadline <= now:
                    due.append((target_id, key[1], event))
                    del self._pending[key]
        for target_id, service_name, event in due:
            try:
                self._publisher(target_id, service_name, event)
                self._published += 1
            except Exception as exc:  # noqa: BLE001 - 单条投递失败不能拖垮总线
                self._dropped += 1
                self._log.warning("事件投递失败 target=%s service=%s err=%s", target_id, service_name, exc)

    def flush_now(self) -> int:
        """立即投递所有待发事件（测试与优雅停机用），返回成功投递条数。"""
        with self._lock:
            items = [(tid, key[1], ev) for key, (_deadline, tid, ev) in self._pending.items()]
            self._pending.clear()
        for target_id, service_name, event in items:
            try:
                self._publisher(target_id, service_name, event)
                self._published += 1
            except Exception as exc:  # noqa: BLE001
                self._dropped += 1
                self._log.warning("事件投递失败 target=%s service=%s err=%s", target_id, service_name, exc)
        return len(items)

    @property
    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"published": self._published, "dropped": self._dropped,
                    "pending": len(self._pending)}
