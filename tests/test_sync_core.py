"""同步核心：差异计算与单服务同步（不依赖 funboost / 数据库）。"""


def test_is_excluded(sync_core):
    assert sync_core.is_excluded("mall-admin", ["mall-.*"]) is True
    assert sync_core.is_excluded("order-svc", ["mall-.*"]) is False
    assert sync_core.is_excluded("order-svc", ["["]) is False  # 非法正则不应抛异常


def test_compute_diff_add_remove_weight(sync_core):
    I = sync_core.Instance
    discovery = [I(ip="1.1.1.1", port=80, weight=1.0), I(ip="2.2.2.2", port=80, weight=1.0)]
    gateway = [I(ip="1.1.1.1", port=80, weight=2.0), I(ip="3.3.3.3", port=80, weight=1.0)]
    diff = sync_core.compute_diff(discovery, gateway)
    by_ip = {d.ip: d for d in diff}
    assert set(by_ip) == {"1.1.1.1", "2.2.2.2", "3.3.3.3"}
    assert by_ip["1.1.1.1"].enabled is True      # 权重变化，注册中心侧仍存在
    assert by_ip["2.2.2.2"].enabled is True      # 仅注册中心有 -> 网关需新增
    assert by_ip["3.3.3.3"].enabled is False     # 仅网关有 -> 需删除
    assert all(d.change for d in diff)
    # 关键回归：不能像旧实现那样就地 _replace 污染上游快照
    assert all(not d.change for d in discovery)
    assert all(not d.change for d in gateway)


def test_compute_diff_no_change(sync_core):
    I = sync_core.Instance
    items = [I(ip="1.1.1.1", port=80, weight=1.0)]
    assert sync_core.compute_diff(items, [I(ip="1.1.1.1", port=80, weight=1.0)]) == []


def test_sync_one_service_updates_gateway(sync_core):
    I = sync_core.Instance
    calls, heartbeats = [], []

    class FakeDiscovery:
        def get_service_all_instances(self, name, config):
            calls.append(("discovery", name))
            return [I(ip="1.1.1.1", port=80, weight=1.0)], 0

    class FakeGateway:
        def get_service_all_instances(self, target, name):
            calls.append(("gateway", name))
            return []

        def sync_instances(self, target, name, diff, instances):
            calls.append(("sync", name, len(diff), len(instances)))

    target = {"id": "t1", "config": {}, "exclude_service": []}
    result = sync_core.sync_one_service(
        target, "svc-a", FakeDiscovery(), FakeGateway(),
        on_heartbeat=heartbeats.append,
    )
    assert result.changed is True and result.diff_count == 1
    assert ("sync", "svc-a", 1, 1) in calls
    assert heartbeats == ["t1"]


def test_sync_one_service_no_diff_still_heartbeats(sync_core):
    """回归：订阅模式下无变更也必须刷新心跳，否则 /health 会误判失联。"""
    I = sync_core.Instance
    heartbeats = []

    class FakeDiscovery:
        def get_service_all_instances(self, name, config):
            return [I(ip="1.1.1.1", port=80, weight=1.0)], 0

    class FakeGateway:
        def get_service_all_instances(self, target, name):
            return [I(ip="1.1.1.1", port=80, weight=1.0)]

        def sync_instances(self, *args):
            raise AssertionError("无差异时不应调用 sync_instances")

    result = sync_core.sync_one_service(
        {"id": "t1", "config": {}, "exclude_service": []},
        "svc-a", FakeDiscovery(), FakeGateway(), on_heartbeat=heartbeats.append,
    )
    assert result.changed is False and result.diff_count == 0
    assert heartbeats == ["t1"]


def test_sync_one_service_failure_does_not_refresh_heartbeat(sync_core):
    """回归：失败时不能刷新心跳，否则 /health 感知不到同步故障（替代死信队列）。"""
    heartbeats = []

    class FakeDiscovery:
        def get_service_all_instances(self, name, config):
            raise RuntimeError("nacos down")

    class FakeGateway:
        def get_service_all_instances(self, target, name):
            return []

        def sync_instances(self, *args):
            raise AssertionError("上游拉取失败时不应写网关")

    result = sync_core.sync_one_service(
        {"id": "t1", "config": {}, "exclude_service": []},
        "svc-a", FakeDiscovery(), FakeGateway(), on_heartbeat=heartbeats.append,
    )
    assert result.error is not None
    assert heartbeats == []


def test_sync_one_service_excluded_is_skipped(sync_core):
    class NeverCalled:
        def get_service_all_instances(self, *args):
            raise AssertionError("被排除的服务不应发起任何请求")

    result = sync_core.sync_one_service(
        {"id": "t1", "config": {}, "exclude_service": ["mall-.*"]},
        "mall-admin", NeverCalled(), NeverCalled(),
    )
    assert result.skipped is True and result.diff_count == 0
