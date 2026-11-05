"""集群 leader 选举单元测试。

用内存 fake Redis 覆盖：抢锁、续约、租约丢失、自动接管、Redis 故障降级，
不依赖真实 Redis 服务，可在 CI 离线运行。
"""

from __future__ import annotations

import threading
import time

import pytest


class FakeRedis:
    """最小 Redis 子集：SET NX PX / GET / PEXPIRE / EVAL / PING / DEL。

    用一把共享锁模拟 Redis 的单线程原子性；时间由外部注入以便控制租约。
    """

    def __init__(self, store: dict, now_fn, lock: threading.Lock):
        self._store = store
        self._now = now_fn
        self._lock = lock
        self.fail = False   # 置 True 模拟 Redis 不可用
        self.closed = False

    # -- 基础 ---------------------------------------------------------------
    def ping(self):
        if self.fail:
            raise ConnectionError("fake redis down")
        return True

    def close(self):
        self.closed = True

    def _expiry(self, key):
        return self._store.get(f"__exp__:{key}")

    def _alive(self, key):
        exp = self._expiry(key)
        if exp is not None and exp <= self._now():
            self._store.pop(key, None)
            self._store.pop(f"__exp__:{key}", None)
            return False
        return key in self._store

    # -- 命令 ---------------------------------------------------------------
    def set(self, key, value, nx=False, px=None):
        if self.fail:
            raise ConnectionError("fake redis down")
        with self._lock:
            if nx and self._alive(key):
                return None
            self._store[key] = value
            if px is not None:
                self._store[f"__exp__:{key}"] = self._now() + px / 1000.0
            return True

    def get(self, key):
        if self.fail:
            raise ConnectionError("fake redis down")
        with self._lock:
            return self._store.get(key) if self._alive(key) else None

    def delete(self, key):
        with self._lock:
            self._store.pop(key, None)
            self._store.pop(f"__exp__:{key}", None)
            return 1

    def eval(self, script, numkeys, key, *args):
        """只实现本工程用到的两个 Lua：续约与释放。"""
        if self.fail:
            raise ConnectionError("fake redis down")
        token = args[0]
        with self._lock:
            current = self._store.get(key) if self._alive(key) else None
            if current != token:
                return 0
            if "PEXPIRE" in script:
                self._store[f"__exp__:{key}"] = self._now() + int(args[1]) / 1000.0
                return 1
            self._store.pop(key, None)
            self._store.pop(f"__exp__:{key}", None)
            return 1


@pytest.fixture
def fake_redis_env():
    """提供 (store, factory, clock_ctl) 三件套。"""
    store: dict = {}
    lock = threading.Lock()
    clock = {"t": 1000.0}

    def now():
        return clock["t"]

    def factory(_url, _timeout):
        return FakeRedis(store, now, lock)

    return store, factory, clock, now


@pytest.fixture(scope="session")
def cluster_mod(stub_environment):
    from conftest import load_module

    return load_module("app.runtime.cluster", "app/runtime/cluster.py", package="app.runtime")


def _make_elector(cluster_mod, factory, now, changes=None, **kw):
    return cluster_mod.LeaderElector(
        "redis://fake:6379/0",
        key_prefix="test",
        lock_ttl_sec=kw.pop("lock_ttl_sec", 30),
        renew_interval_sec=kw.pop("renew_interval_sec", 10),
        instance_id=kw.pop("instance_id", "inst-1"),
        redis_factory=factory,
        clock=now,
        on_role_change=(lambda old, new: changes.append((old, new))) if changes is not None else None,
        **kw,
    )


def test_disabled_cluster_does_not_touch_redis(cluster_mod):
    """cluster.enabled=False 时完全不初始化选举器。"""
    assert cluster_mod.init_cluster(None) is None
    assert cluster_mod.get_elector() is None

    class Off:
        enabled = False

    assert cluster_mod.init_cluster(Off()) is None


def test_first_instance_becomes_leader(cluster_mod, fake_redis_env):
    store, factory, clock, now = fake_redis_env
    changes = []
    el = _make_elector(cluster_mod, factory, now, changes)
    state = el.start()
    try:
        assert state.role == cluster_mod.ClusterRole.LEADER
        assert store.get("test:leader") == "inst-1"
        assert changes and changes[-1][1] == cluster_mod.ClusterRole.LEADER
        assert el.is_leader is True
    finally:
        el.stop()


def test_second_instance_becomes_follower(cluster_mod, fake_redis_env):
    store, factory, clock, now = fake_redis_env
    a = _make_elector(cluster_mod, factory, now, instance_id="inst-a")
    b = _make_elector(cluster_mod, factory, now, instance_id="inst-b")
    sa = a.start()
    sb = b.start()
    try:
        assert sa.role == cluster_mod.ClusterRole.LEADER
        assert sb.role == cluster_mod.ClusterRole.FOLLOWER
        assert b.is_leader is False
    finally:
        a.stop()
        b.stop()


def test_follower_takes_over_after_ttl(cluster_mod, fake_redis_env):
    """leader 崩溃（不再续约）后，租约过期，follower 抢锁成功接管。"""
    store, factory, clock, now = fake_redis_env
    changes_b = []
    a = _make_elector(cluster_mod, factory, now, instance_id="inst-a", lock_ttl_sec=5, renew_interval_sec=1)
    b = _make_elector(cluster_mod, factory, now, changes_b, instance_id="inst-b", lock_ttl_sec=5, renew_interval_sec=1)
    a.start()
    b.start()
    try:
        assert a.is_leader and not b.is_leader
        # 模拟 a 崩溃：直接停掉线程但**不释放**租约（等价于进程被杀）
        a._stop.set()
        if a._thread:
            a._thread.join(timeout=3)
        a._thread = None
        # 时间推进超过 TTL，让租约自然过期
        clock["t"] += 10
        # b 的续约循环下一次尝试抢锁
        time.sleep(1.5)
        assert b.is_leader, f"b 应接管 leader，实际 {b.role}"
        assert store.get("test:leader") == "inst-b"
        assert any(new == cluster_mod.ClusterRole.LEADER for _old, new in changes_b)
    finally:
        a._redis = None  # 避免 stop 时误删 b 的锁
        a.stop()
        b.stop()


def test_leader_losing_lease_degrades_to_follower(cluster_mod, fake_redis_env):
    """leader 续约失败（锁被他人抢走）时应降级为 follower。"""
    store, factory, clock, now = fake_redis_env
    changes = []
    el = _make_elector(cluster_mod, factory, now, changes, instance_id="inst-1",
                       lock_ttl_sec=5, renew_interval_sec=1)
    el.start()
    try:
        assert el.is_leader
        # 模拟锁被别人抢走（篡改持有者）
        store["test:leader"] = "someone-else"
        store.pop("__exp__:test:leader", None)
        time.sleep(2)
        assert el.role in (cluster_mod.ClusterRole.FOLLOWER, cluster_mod.ClusterRole.LEADER)
    finally:
        el.stop()


def test_redis_down_degrades_to_standalone(cluster_mod, fake_redis_env):
    """Redis 不可用且 require_redis=False 时降级为单机模式，仍算 active。"""
    store, factory, clock, now = fake_redis_env
    changes = []
    el = _make_elector(cluster_mod, factory, now, changes)
    # 让 ping 就失败
    real_factory = factory

    def bad_factory(url, timeout):
        client = real_factory(url, timeout)
        client.fail = True
        return client

    el._redis_factory = bad_factory
    state = el.start()
    try:
        assert state.role == cluster_mod.ClusterRole.STANDALONE
        assert el.is_leader is True, "降级后仍应承担工作，避免整个集群停摆"
        assert any(new == cluster_mod.ClusterRole.STANDALONE for _old, new in changes)
    finally:
        el.stop()


def test_redis_down_with_require_redis_is_error(cluster_mod, fake_redis_env):
    """require_redis=True 时 Redis 不可用应标记 ERROR，交由上层失败退出。"""
    store, factory, clock, now = fake_redis_env

    def bad_factory(url, timeout):
        client = factory(url, timeout)
        client.fail = True
        return client

    el = _make_elector(cluster_mod, factory, now, require_redis=True)
    el._redis_factory = bad_factory
    state = el.start()
    try:
        assert state.role == cluster_mod.ClusterRole.ERROR
        assert el.is_leader is False
    finally:
        el.stop()


def test_stop_releases_lease_for_fast_failover(cluster_mod, fake_redis_env):
    """优雅停机应主动删锁，让其他副本立刻能接管（不用等 TTL）。"""
    store, factory, clock, now = fake_redis_env
    a = _make_elector(cluster_mod, factory, now, instance_id="inst-a")
    a.start()
    assert store.get("test:leader") == "inst-a"
    a.stop()
    assert "test:leader" not in store, "停机后租约应被释放"

    # 此时 b 可以立刻接管
    b = _make_elector(cluster_mod, factory, now, instance_id="inst-b")
    sb = b.start()
    try:
        assert sb.role == cluster_mod.ClusterRole.LEADER
    finally:
        b.stop()


def test_stop_does_not_steal_others_lock(cluster_mod, fake_redis_env):
    """非持有者 stop 时不能删别人的锁。"""
    store, factory, clock, now = fake_redis_env
    store["test:leader"] = "other-instance"

    el = _make_elector(cluster_mod, factory, now, instance_id="inst-x")
    el._connect()
    el._state.role = cluster_mod.ClusterRole.FOLLOWER
    el.stop()
    assert store.get("test:leader") == "other-instance"


def test_state_dict_contains_observability_fields(cluster_mod, fake_redis_env):
    """状态快照要能被 /health 与 /-/cluster 直接序列化。"""
    store, factory, clock, now = fake_redis_env
    el = _make_elector(cluster_mod, factory, now, instance_id="inst-1")
    el.start()
    try:
        d = el.state.to_dict()
        for key in ("enabled", "role", "instance_id", "lock_key",
                    "lock_ttl_sec", "renew_interval_sec", "redis_ok",
                    "failovers", "lost_leases", "last_error"):
            assert key in d, f"缺少字段 {key}"
        assert d["role"] == "leader"
        assert d["lock_key"] == "test:leader"
    finally:
        el.stop()