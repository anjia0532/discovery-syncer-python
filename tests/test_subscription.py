"""订阅层：UDP 包解析、去抖、快照 diff、webhook、UDP 续约、自动降级。"""

import gzip
import json
import time
from types import SimpleNamespace


def _svc(name, instances):
    return SimpleNamespace(name=name, instances=instances)


def _ins(ip, port=80, weight=1.0, enabled=True):
    return SimpleNamespace(ip=ip, port=port, weight=weight, enabled=enabled)


def test_parse_nacos_push_plain_dom(sub_providers):
    raw = json.dumps({
        "type": "dom",
        "data": json.dumps({"name": "DEFAULT_GROUP@@svc-a", "groupName": "DEFAULT_GROUP",
                            "namespaceId": "ns1", "hosts": []}),
    }).encode()
    event = sub_providers.parse_nacos_push(raw)
    assert event.service_name == "svc-a"
    assert event.group == "DEFAULT_GROUP"
    assert event.namespace == "ns1"
    assert event.source == "udp"


def test_parse_nacos_push_gzip_and_direct(sub_providers):
    raw = json.dumps({"type": "service", "data": json.dumps({"serviceName": "svc-b"})}).encode()
    assert sub_providers.parse_nacos_push(gzip.compress(raw)).service_name == "svc-b"
    direct = json.dumps({"name": "svc-c", "groupName": "G2"}).encode()
    assert sub_providers.parse_nacos_push(direct).service_name == "svc-c"
    assert sub_providers.parse_nacos_push(b"not-json") is None
    assert sub_providers.parse_nacos_push(b"") is None


def test_fingerprint_detects_weight_change(sub_providers):
    a = sub_providers.fingerprint_instances([_ins("1.1.1.1", 80, 1.0), _ins("2.2.2.2", 80, 1.0)])
    b = sub_providers.fingerprint_instances([_ins("2.2.2.2", 80, 1.0), _ins("1.1.1.1", 80, 1.0)])
    c = sub_providers.fingerprint_instances([_ins("1.1.1.1", 80, 2.0), _ins("2.2.2.2", 80, 1.0)])
    assert a == b          # 顺序无关
    assert a != c          # 权重变化可感知


def test_event_bus_debounce(sub_core):
    published = []
    bus = sub_core.EventBus(debounce_ms=30,
                            publisher=lambda tid, name, ev: published.append((tid, name, ev.source)))
    bus.start()
    for source in ("udp", "udp", "grpc"):
        bus.emit("t1", sub_core.ChangeEvent(service_name="svc-a", source=source))
    time.sleep(0.2)
    bus.stop()
    assert len(published) == 1, f"同服务连续事件应合并为一次，实际 {published}"
    assert published[0][0] == "t1" and published[0][1] == "svc-a"
    assert published[0][2] == "grpc"  # 保留窗口内最后一条


def test_event_bus_separate_services(sub_core):
    published = []
    bus = sub_core.EventBus(debounce_ms=10,
                            publisher=lambda tid, name, ev: published.append(name))
    bus.start()
    bus.emit("t1", sub_core.ChangeEvent(service_name="svc-a"))
    bus.emit("t1", sub_core.ChangeEvent(service_name="svc-b"))
    time.sleep(0.15)
    bus.stop()
    assert sorted(published) == ["svc-a", "svc-b"]


def test_event_bus_queue_full_drops_instead_of_blocking(sub_core):
    """队列满时必须丢弃而非阻塞上游推送线程（避免内存无界增长）。"""
    bus = sub_core.EventBus(debounce_ms=1000, publisher=lambda *a: None, max_queue=2)
    # 不启动消费线程：队列不会排空，第 3 条必然满
    for i in range(5):
        bus.emit("t1", sub_core.ChangeEvent(service_name=f"svc-{i}"))
    assert bus.stats["dropped"] == 3, f"应丢弃 3 条，实际 {bus.stats}"
    assert bus._queue.qsize() == 2


def test_event_bus_stats_has_pending(sub_core):
    bus = sub_core.EventBus(debounce_ms=1000, publisher=lambda *a: None)
    bus.emit("t1", sub_core.ChangeEvent(service_name="svc-a"))
    assert bus.stats["pending"] >= 0
    assert set(bus.stats) == {"published", "dropped", "pending"}


def test_snapshot_polling_only_emits_on_change(sub_providers):
    events = []
    state = {"items": [_ins("1.1.1.1", 80)]}
    provider = sub_providers.SnapshotPollingSubscription(
        target_id="t1", discovery="nacos1", emit=events.append,
        snapshot_provider=lambda: [_svc("svc-a", state["items"])],
        interval_sec=60, emit_initial=True,
    )
    assert provider.poll_once() == 1       # 首次全量对齐
    assert provider.poll_once() == 0       # 无变化 -> 不触碰下游
    state["items"] = [_ins("1.1.1.1", 80, 2.0)]
    assert provider.poll_once() == 1       # 权重变化
    assert [e.service_name for e in events] == ["svc-a", "svc-a"]


def test_webhook_notify(sub_providers):
    events = []
    hook = sub_providers.WebhookSubscription(target_id="t1", discovery="nacos1", emit=events.append)
    hook.start()
    assert hook.notify({"serviceName": "svc-a", "groupName": "G", "namespaceId": "public"}) == 1
    assert hook.notify({"services": ["svc-a", "svc-b", "svc-a"]}) == 2
    assert hook.notify({}) == 0
    assert events[0].group == "G" and events[0].namespace == "public"
    assert events[0].source == "webhook"


def test_udp_refresh_resubscribes_all_services(sub_providers):
    """v3.0.2：每轮 refresh 都对**所有**服务重发订阅（真实续约），
    返回值只统计**新发现**的服务数。旧实现对新服务之外的直接 skip，
    订阅被服务端清理后永不再登记，属静默中断隐患。"""
    events, calls = [], []
    services = [_svc("svc-a", [_ins("1.1.1.1", 80)]), _svc("svc-b", [_ins("2.2.2.2", 80)])]
    provider = sub_providers.NacosUdpSubscription(
        target_id="t1", discovery="nacos1", emit=events.append,
        list_services=lambda: services,
        send_subscribe=lambda name, group, ns, port: calls.append((name, group, ns, port)),
        refresh_interval_sec=30,
    )
    provider._udp_port = 19999

    # 第一轮：发现 2 个新服务，各发一次订阅
    assert provider.refresh_subscriptions() == 2
    assert len(calls) == 2 and all(c[3] == 19999 for c in calls)
    assert {e.service_name for e in events} == {"svc-a", "svc-b"}

    # 第二轮：added=0（无新服务），但**续约仍重发**——这是本次修复的核心
    assert provider.refresh_subscriptions() == 0
    assert len(calls) == 4, f"已订阅服务应被重新登记（续约），实际 calls={len(calls)}"
    # 续约不应产出新事件（否则对账/事件会被重复触发）
    assert {e.service_name for e in events} == {"svc-a", "svc-b"}
    assert len(events) == 2, "续约不应重复产出事件"
    assert provider.health().alive is True


def test_udp_push_timeout_marks_degraded_without_refresh(sub_providers):
    """失联判定应在 health() 实时计算，而非只在 refresh 里算。

    回归点：refresh 默认周期改为 5m 后，若失联判定仍绑在 refresh 上，
    降级检测会滞后一个 refresh 周期（最坏 5m），supervisor 每 5s 巡检
    看到的 degraded 会长期为假阴性。
    """
    clock = {"t": 1000.0}
    services = [_svc("svc-a", [_ins("1.1.1.1", 80)])]
    provider = sub_providers.NacosUdpSubscription(
        target_id="t1", discovery="nacos1", emit=lambda ev: None,
        list_services=lambda: services,
        send_subscribe=lambda *a: None,
        push_timeout_sec=60,
        clock=lambda: clock["t"],
    )
    # 模拟已启动（start() 会设 _started_at），但未收到任何真实推送
    provider._started_at = clock["t"]
    provider._health.alive = True

    assert provider.health().degraded is False, "刚启动在 push_timeout 内不应判降级"

    # 时间推进超过 push_timeout，未收到推送 -> 实时判降级
    clock["t"] += 61
    assert provider.health().degraded is True
    assert "未收到 UDP 推送" in provider.health().reason

    # 收到真实推送后，降级标记自动清除
    provider._record_push(sub_providers.ChangeEvent(service_name="svc-a", discovery="nacos1"))
    assert provider.health().degraded is False


def test_udp_startup_grace_before_first_push(sub_providers):
    """last_push_at=None 时以 _started_at 计时，避免刚启动就被误判失联。"""
    clock = {"t": 5000.0}
    provider = sub_providers.NacosUdpSubscription(
        target_id="t1", discovery="nacos1", emit=lambda ev: None,
        list_services=lambda: [], send_subscribe=lambda *a: None,
        push_timeout_sec=120, clock=lambda: clock["t"],
    )
    provider._started_at = clock["t"]
    # 启动后 100s，仍在 120s 宽限内且无任何推送
    clock["t"] += 100
    assert provider.health().degraded is False
    # 超过 120s 仍无真实推送 -> 判失联
    clock["t"] += 30
    assert provider.health().degraded is True


def test_udp_health_before_start_is_not_degraded(sub_providers):
    """未 start()（_started_at=0）时不做失联判定，避免纯逻辑构造被误判。"""
    clock = {"t": 90000.0}
    provider = sub_providers.NacosUdpSubscription(
        target_id="t1", discovery="nacos1", emit=lambda ev: None,
        list_services=lambda: [], send_subscribe=lambda *a: None,
        push_timeout_sec=120, clock=lambda: clock["t"],
    )
    assert provider._started_at == 0.0
    assert provider.health().degraded is False, "未启动不应判失联"


def test_manager_auto_fallback_on_degraded_push(sub_manager, sub_core):
    manager = sub_manager.SubscriptionManager(
        publisher=lambda *args: None, supervise_interval=60,
    )
    spec = sub_manager.TargetSpec(
        target_id="t1", discovery="nacos3", mode=sub_core.SubscribeMode.AUTO,
        push_transport="grpc", major=3, label="nacos-3.x",
        webhook_enabled=False, fallback_enabled=True,
        snapshot_provider=lambda: [],
    )
    manager.rebuild([spec])
    degraded = manager.supervise_once()
    status = manager.status("t1")
    assert "t1" in degraded
    assert status["fallback_added"] is True
    assert any(p["kind"] == "polling" for p in status["providers"])
    assert status["degraded"] is True
    manager.stop_all()


def test_manager_off_mode_has_no_providers(sub_manager, sub_core):
    manager = sub_manager.SubscriptionManager(supervise_interval=60)
    spec = sub_manager.TargetSpec(
        target_id="t1", discovery="nacos1", mode=sub_core.SubscribeMode.OFF,
        push_transport="udp", major=1, label="nacos-1.x",
        webhook_enabled=False,
    )
    manager.rebuild([spec])
    assert manager.status("t1")["providers"] == []
    manager.stop_all()


def test_manager_webhook_dispatch(sub_manager, sub_core):
    manager = sub_manager.SubscriptionManager(supervise_interval=60)
    spec = sub_manager.TargetSpec(
        target_id="t1", discovery="nacos1", mode=sub_core.SubscribeMode.WEBHOOK,
        webhook_enabled=True,
    )
    manager.rebuild([spec])
    assert manager.handle_webhook("nacos1", {"serviceName": "svc-a"}) == 1
    assert manager.handle_webhook("unknown", {"serviceName": "svc-a"}) == 0
    assert manager.status("t1")["event_count"] == 1
    manager.stop_all()
