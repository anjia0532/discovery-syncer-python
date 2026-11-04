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


def test_udp_refresh_subscribes_new_services_only(sub_providers):
    events, calls = [], []
    services = [_svc("svc-a", [_ins("1.1.1.1", 80)]), _svc("svc-b", [_ins("2.2.2.2", 80)])]
    provider = sub_providers.NacosUdpSubscription(
        target_id="t1", discovery="nacos1", emit=events.append,
        list_services=lambda: services,
        send_subscribe=lambda name, group, ns, port: calls.append((name, group, ns, port)),
        refresh_interval_sec=30,
    )
    provider._udp_port = 19999
    assert provider.refresh_subscriptions() == 2
    assert len(calls) == 2 and all(c[3] == 19999 for c in calls)
    assert provider.refresh_subscriptions() == 0   # 已订阅的服务不重复发
    assert provider.health().alive is True
    assert {e.service_name for e in events} == {"svc-a", "svc-b"}


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
