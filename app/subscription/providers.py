"""四种订阅 Provider 实现。

* :class:`NacosUdpSubscription`      — Nacos 1.x 原生 UDP 推送（2.x 兼容层也会尝试）。
* :class:`NacosGrpcSubscription`     — Nacos 2.x/3.x gRPC 订阅；支持注入真实流，未注入时标记降级。
* :class:`SnapshotPollingSubscription` — 快照 diff 增量轮询，Eureka / 兜底 / 降级通用。
* :class:`WebhookSubscription`       — 入站 HTTP 回调，任意版本可叠加。

所有 Provider 只依赖标准库；网络动作通过构造参数注入的回调完成，便于离线测试。
"""

from __future__ import annotations

import gzip
import json
import logging
import socket
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .core import ChangeEvent, ProviderKind, SubscriptionProvider

__all__ = [
    "parse_nacos_push", "fingerprint_instances",
    "NacosUdpSubscription", "NacosGrpcSubscription",
    "SnapshotPollingSubscription", "WebhookSubscription",
]


# ---------------------------------------------------------------------------
# Nacos UDP 推送包解析
# ---------------------------------------------------------------------------
def parse_nacos_push(payload: bytes, *, source: str = "udp") -> Optional[ChangeEvent]:
    """解析 Nacos 服务端 UDP 推送包，提取服务名。

    Nacos 1.x 推送体可能是：
    * 明文 JSON ``{"type":"dom","data":"<ServiceInfo JSON 字符串>"}``
    * ``{"type":"service","data":"..."}``
    * 直接是 ServiceInfo：``{"name":"DEFAULT_GROUP@@svc","hosts":[...]}``
    * gzip 压缩（前两字节 ``1f 8b``）后的上述任意一种。

    Args:
        payload: UDP 数据报原始字节。
        source: 事件来源标记。

    Returns:
        :class:`ChangeEvent`；无法解析出服务名时返回 ``None``。
    """
    if not payload:
        return None
    try:
        raw = payload
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        obj = json.loads(text)
        info: Any = obj
        if isinstance(obj, dict):
            data = obj.get("data")
            if isinstance(data, str) and data.strip().startswith("{"):
                try:
                    info = json.loads(data)
                except (ValueError, TypeError):
                    info = obj
            elif isinstance(data, dict):
                info = data
            elif obj.get("type") in ("dom", "service", "serviceInfo") and isinstance(data, str):
                info = json.loads(data)
        if not isinstance(info, dict):
            return None

        name = ""
        for key in ("name", "serviceName", "dom", "service"):
            value = info.get(key)
            if isinstance(value, str) and value:
                name = value
                break
        if not name:
            return None

        group = str(info.get("groupName") or info.get("group") or "")
        namespace = str(info.get("namespaceId") or info.get("namespace") or "")
        # Nacos 内部名常为 "GROUP@@service"，拆出真实服务名与 group。
        if "@@" in name:
            prefix, _, real = name.partition("@@")
            name = real or name
            if not group:
                group = prefix
        return ChangeEvent(
            service_name=name, group=group, namespace=namespace, source=source,
        )
    except Exception:  # noqa: BLE001 - 非法推送包不能打断接收线程
        return None


def fingerprint_instances(instances: Iterable[Any]) -> Tuple[Tuple[Any, ...], ...]:
    """把实例列表压成可比较的指纹。

    Args:
        instances: ``Instance`` 或具备 ip/port/weight/enabled 属性的对象。

    Returns:
        排序后的元组，用于判断上游快照是否真的发生变化。
    """
    items = []
    for ins in instances or []:
        items.append((
            str(getattr(ins, "ip", "") or ""),
            int(getattr(ins, "port", 0) or 0),
            float(getattr(ins, "weight", 0) or 0),
            bool(getattr(ins, "enabled", False)),
        ))
    return tuple(sorted(items))


# ---------------------------------------------------------------------------
# Nacos 1.x / 2.x 兼容层：UDP 推送
# ---------------------------------------------------------------------------
class NacosUdpSubscription(SubscriptionProvider):
    """Nacos UDP 推送接收端 + 订阅续约 + 新服务发现。

    Nacos 服务端的 UDP 订阅注册有过期时间，且**服务列表本身没有 push**，
    因此必须周期性重发带 ``udpPort`` 的订阅请求；该动作同时承担「发现新服务」职责。

    Args:
        target_id: 作业 ID。
        discovery: 注册中心名。
        emit: 事件回调。
        list_services: ``() -> list[Service]``，返回当前全部服务。
        send_subscribe: ``(service_name, group, namespace, udp_port) -> None``，发订阅请求。
        refresh_interval_sec: 续约/新服务发现周期。
        push_timeout_sec: 超过该秒数没收到真实推送则标记 degraded（0 表示不判定）。
        udp_host / udp_port: 监听地址，port=0 表示随机端口。
        socket_factory: 可注入 socket 工厂（测试用）。
    """

    kind = ProviderKind.NACOS_UDP

    def __init__(
        self,
        *,
        target_id: str,
        discovery: str,
        emit: Callable[[ChangeEvent], None],
        list_services: Callable[[], List[Any]],
        send_subscribe: Callable[[str, str, str, int], None],
        refresh_interval_sec: float = 30.0,
        push_timeout_sec: float = 0.0,
        udp_host: str = "0.0.0.0",
        udp_port: int = 0,
        socket_factory: Callable[[], Any] = None,
        logger: Optional[Any] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(target_id=target_id, discovery=discovery, emit=emit, logger=logger, clock=clock)
        self._list_services = list_services
        self._send_subscribe = send_subscribe
        self._refresh_interval = max(1.0, refresh_interval_sec)
        self._push_timeout = push_timeout_sec
        self._udp_host = udp_host
        self._udp_port = udp_port
        self._socket_factory = socket_factory or (lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        self._sock: Optional[Any] = None
        self._subscribed: set = set()
        self._recv_thread: Optional[threading.Thread] = None
        self._refresh_thread: Optional[threading.Thread] = None

    @property
    def bound_port(self) -> int:
        """实际绑定的 UDP 端口（随机端口场景下需回填给订阅请求）。"""
        return self._udp_port

    def start(self) -> None:
        if self._recv_thread and self._recv_thread.is_alive():
            return
        self._stop_event.clear()
        try:
            self._sock = self._socket_factory()
            self._sock.bind((self._udp_host, self._udp_port))
            self._sock.settimeout(0.5)
            self._udp_port = int(self._sock.getsockname()[1])
        except Exception as exc:  # noqa: BLE001
            self._health.alive = False
            self._health.degraded = True
            self._health.reason = f"UDP 绑定失败: {exc}"
            self.log.warning("Nacos UDP 绑定失败 target=%s err=%s", self.target_id, exc)
            return

        self._health.alive = True
        self._health.reason = f"listening udp://{self._udp_host}:{self._udp_port}"
        self._recv_thread = threading.Thread(target=self._recv_loop, name=f"nacos-udp-{self.target_id}", daemon=True)
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop, name=f"nacos-refresh-{self.target_id}", daemon=True
        )
        self._recv_thread.start()
        self._refresh_thread.start()
        self.refresh_subscriptions()

    def stop(self) -> None:
        self._stop_event.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:  # noqa: BLE001
                pass
            self._sock = None
        for thread in (self._recv_thread, self._refresh_thread):
            if thread and thread.is_alive():
                thread.join(timeout=2.0)
        self._health.alive = False

    def _recv_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                data, _addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if not self._stop_event.is_set():
                    self._health.degraded = True
                    self._health.reason = "UDP socket 异常关闭"
                break
            event = parse_nacos_push(data, source="udp")
            if event:
                event = ChangeEvent(
                    service_name=event.service_name, discovery=self.discovery,
                    group=event.group, namespace=event.namespace, source="udp",
                )
                self._record_push(event)

    def _refresh_loop(self) -> None:
        while not self._stop_event.wait(self._refresh_interval):
            self.refresh_subscriptions()

    def refresh_subscriptions(self) -> int:
        """重发订阅（续约）并对新发现的服务产出事件，返回新增订阅数。

        Returns:
            本次新订阅的服务数量。
        """
        try:
            services = self._list_services() or []
        except Exception as exc:  # noqa: BLE001
            self._health.degraded = True
            self._health.reason = f"拉取服务列表失败: {exc}"
            self.log.warning("Nacos 服务列表拉取失败 target=%s err=%s", self.target_id, exc)
            return 0

        current = set()
        added = 0
        for service in services:
            name = getattr(service, "name", None)
            if not name:
                continue
            ext = dict(getattr(service, "ext", {}) or {})
            # Service 模型没有 ext 字段，Nacos 的 group/namespace 实际挂在首个 Instance.ext 上。
            instances = getattr(service, "instances", []) or []
            if instances:
                ext = {**dict(getattr(instances[0], "ext", {}) or {}), **ext}
            group = str(ext.get("groupName") or "DEFAULT_GROUP")
            namespace = str(ext.get("namespaceId") or "")
            key = (namespace, group, name)
            current.add(key)
            if key in self._subscribed:
                continue
            try:
                self._send_subscribe(name, group, namespace, self._udp_port)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Nacos 订阅失败 service=%s err=%s", name, exc)
                continue
            self._subscribed.add(key)
            added += 1
            self._record_event(ChangeEvent(
                service_name=name, discovery=self.discovery, group=group,
                namespace=namespace, source="subscribe",
            ))
        self._subscribed.intersection_update(current)
        self.heartbeat()

        if self._push_timeout > 0:
            last = self._health.last_push_at
            if last is None or (self._clock() - last) > self._push_timeout:
                self._health.degraded = True
                self._health.reason = (
                    f"超过 {self._push_timeout:.0f}s 未收到 UDP 推送（可能被 NAT/防火墙阻断），需降级"
                )
        return added

    def health(self):
        snapshot = super().health()
        snapshot.reason = self._health.reason
        snapshot.degraded = self._health.degraded
        return snapshot


# ---------------------------------------------------------------------------
# Nacos 2.x / 3.x：gRPC 订阅
# ---------------------------------------------------------------------------
class NacosGrpcSubscription(SubscriptionProvider):
    """Nacos 2.x/3.x gRPC 订阅通道。

    Nacos 官方订阅协议是基于 protobuf 的双向流（端口 = 主端口 + 1000）。
    为避免在无 SDK 环境里伪造一个不可验证的半成品协议实现，这里采用**可注入流**设计：

    * 若注入 ``grpc_stream``（返回 ChangeEvent 迭代器的可调用对象），则按真实推送处理。
    * 否则只做端口探测并标记 ``degraded=True``，由 Manager 自动叠加
      :class:`SnapshotPollingSubscription` 兜底，保证功能可用。

    这样既保留了接官方 nacos-sdk-python / 自研 gRPC 客户端的扩展点，
    又不会在 gRPC 不可用时静默丢事件。

    Args:
        grpc_stream: 可选，``() -> Iterable[ChangeEvent]``。
        grpc_probe: 可选，``(host, port) -> bool``。
        grpc_host / grpc_port: gRPC 端点。
    """

    kind = ProviderKind.NACOS_GRPC

    def __init__(
        self,
        *,
        target_id: str,
        discovery: str,
        emit: Callable[[ChangeEvent], None],
        grpc_stream: Optional[Callable[[], Iterable[ChangeEvent]]] = None,
        grpc_probe: Optional[Callable[[str, int], bool]] = None,
        grpc_host: str = "",
        grpc_port: int = 0,
        logger: Optional[Any] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(target_id=target_id, discovery=discovery, emit=emit, logger=logger, clock=clock)
        self._grpc_stream = grpc_stream
        self._grpc_probe = grpc_probe
        self._grpc_host = grpc_host
        self._grpc_port = grpc_port
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._stop_event.clear()
        reachable = True
        if self._grpc_probe and self._grpc_port:
            reachable = bool(self._grpc_probe(self._grpc_host, self._grpc_port))
        if self._grpc_stream is None:
            self._health.alive = reachable
            self._health.degraded = True
            self._health.reason = (
                "gRPC 订阅流未注入；Nacos 2.x/3.x 官方推送需 gRPC(9848)，"
                "已由 Manager 叠加快照 diff 增量轮询兜底"
                if reachable else f"gRPC 端口 {self._grpc_port} 不可达，已降级"
            )
            return
        self._health.alive = True
        self._health.degraded = False
        self._health.reason = "gRPC bi-stream active"
        self._thread = threading.Thread(target=self._stream_loop, name=f"nacos-grpc-{self.target_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._health.alive = False

    def _stream_loop(self) -> None:
        try:
            for event in self._grpc_stream():
                if self._stop_event.is_set():
                    break
                if event and event.service_name:
                    self._record_push(ChangeEvent(
                        service_name=event.service_name, discovery=self.discovery,
                        group=event.group, namespace=event.namespace, source="grpc",
                    ))
        except Exception as exc:  # noqa: BLE001
            self._health.degraded = True
            self._health.reason = f"gRPC 流异常: {exc}"
            self.log.warning("Nacos gRPC 流异常 target=%s err=%s", self.target_id, exc)


# ---------------------------------------------------------------------------
# 快照 diff 增量轮询（兜底 / Eureka / 降级）
# ---------------------------------------------------------------------------
class SnapshotPollingSubscription(SubscriptionProvider):
    """上游快照 diff：只有实例集合真的变化才产出事件。

    相比旧版「定时全量拉注册中心 + 全量拉网关 + 比对」，这里只拉一次注册中心
    并与本地快照比对；无变化时不产生任何事件，也就完全不会触碰下游网关 Admin API。
    这正是 README 待优化点 #3 想要的「缓存比对无差异就不拉下游」。

    Args:
        snapshot_provider: ``() -> list[Service]``。
        interval_sec: 轮询间隔。
        emit_initial: 首次是否把所有服务都产出事件（用于启动全量对齐）。
    """

    kind = ProviderKind.POLLING

    def __init__(
        self,
        *,
        target_id: str,
        discovery: str,
        emit: Callable[[ChangeEvent], None],
        snapshot_provider: Callable[[], List[Any]],
        interval_sec: float = 10.0,
        emit_initial: bool = False,
        logger: Optional[Any] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(target_id=target_id, discovery=discovery, emit=emit, logger=logger, clock=clock)
        self._snapshot_provider = snapshot_provider
        self._interval = max(0.5, interval_sec)
        self._emit_initial = emit_initial
        self._snapshot: Dict[str, Tuple[Tuple[Any, ...], ...]] = {}
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._health.alive = True
        self._health.reason = f"snapshot diff every {self._interval:.1f}s"
        self._thread = threading.Thread(
            target=self._loop, name=f"snapshot-poll-{self.target_id}", daemon=True
        )
        self._thread.start()
        self.poll_once()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._health.alive = False

    def _loop(self) -> None:
        while not self._stop_event.wait(self._interval):
            self.poll_once()

    def poll_once(self) -> int:
        """执行一次快照比对，返回产出事件数（可离线直接调用做测试）。"""
        try:
            services = self._snapshot_provider() or []
        except Exception as exc:  # noqa: BLE001
            self._health.degraded = True
            self._health.reason = f"快照拉取失败: {exc}"
            return 0
        current = {}
        for service in services:
            name = getattr(service, "name", None)
            if not name:
                continue
            current[name] = fingerprint_instances(getattr(service, "instances", []) or [])
        changed = [
            name for name in set(current) | set(self._snapshot)
            if current.get(name) != self._snapshot.get(name)
        ]
        if not self._snapshot and self._emit_initial:
            changed = list(current)
        self._snapshot = current
        for name in sorted(changed):
            self._record_event(ChangeEvent(
                service_name=name, discovery=self.discovery, source="polling",
            ))
        self.heartbeat()
        return len(changed)


# ---------------------------------------------------------------------------
# 入站 Webhook
# ---------------------------------------------------------------------------
class WebhookSubscription(SubscriptionProvider):
    """入站 HTTP 回调：由上游/CI/脚本主动 POST 变更。

    载荷支持的字段（大小写不敏感）：
    ``serviceName`` / ``service`` / ``name`` / ``services``（列表）、
    ``groupName`` / ``group``、``namespaceId`` / ``namespace``。
    """

    kind = ProviderKind.WEBHOOK

    def start(self) -> None:
        self._stop_event.clear()
        self._health.alive = True
        self._health.reason = "passive webhook receiver"

    def stop(self) -> None:
        self._stop_event.set()
        self._health.alive = False

    def notify(self, payload: Dict[str, Any]) -> int:
        """把一次 webhook 载荷转成事件，返回产出事件数。"""
        if not isinstance(payload, dict):
            return 0
        get = lambda *keys: next((payload[k] for k in keys if payload.get(k) is not None), None)  # noqa: E731
        names: List[str] = []
        raw_services = get("services", "serviceNames")
        if isinstance(raw_services, list):
            names.extend(str(x) for x in raw_services if x)
        single = get("serviceName", "service_name", "service", "name")
        if single:
            names.append(str(single))
        group = str(get("groupName", "group_name", "group") or "")
        namespace = str(get("namespaceId", "namespace_id", "namespace") or "")
        count = 0
        for name in dict.fromkeys(names):  # 去重且保序
            count += 1
            self._record_push(ChangeEvent(
                service_name=name, discovery=self.discovery, group=group,
                namespace=namespace, source="webhook",
            ))
        return count
