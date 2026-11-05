"""集群模式：基于 Redis 租约的 leader 选举。

设计要点
--------
本服务持有大量**进程内状态**（HTTP 客户端字典、订阅 Provider、UDP socket、
APScheduler 作业表），多副本同时运行会重复写网关。因此集群化的正确做法不是
共享状态，而是**只让一个副本干活**（leader），其余副本待命并可自动接管。

选举用 Redis 单键租约：

* 抢锁：``SET key <instance_id> NX PX <ttl_ms>`` —— 原子，天然互斥。
* 续约：``PEXPIRE key <ttl_ms>`` 仅当 ``GET key == instance_id`` 时执行
  （用 Lua 保证「校验持有者 + 续期」的原子性，避免把别人的锁续上）。
* 释放：``GET key == instance_id`` 时 ``DEL``，避免误删新任 leader 的锁。
* 租约过期即视为 leader 失联，其他副本在下一次抢锁时自动接管。

Redis 不可用时的策略由 ``require_redis`` 决定：

* ``False``（默认）：降级为「单机模式」，本副本自认 leader 继续服务。
  适合从单副本平滑迁移；但**多副本下会重复写网关**，生产集群请置 True。
* ``True``：视为致命错误，由上层让启动失败/退出，避免静默脑裂。

实现只依赖 ``redis`` 包的同步客户端，且不在此模块顶层 import，
以便在未安装 redis 的环境里做纯逻辑单元测试（可注入 fake client）。
"""

from __future__ import annotations

import logging
import os
import random
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

__all__ = ["ClusterRole", "LeaderElector", "get_elector", "init_cluster", "shutdown_cluster"]

LOG = logging.getLogger("syncer.cluster")

# 「校验持有者 + 续期」的 Lua：只有仍持有锁才续期，避免续上别人的锁。
_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

# 「校验持有者 + 删除」的 Lua：只有仍持有锁才释放，避免误删新任 leader 的锁。
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class ClusterRole(str, Enum):
    """副本角色。"""

    STANDALONE = "standalone"  # 未启用集群，或 Redis 不可用降级
    LEADER = "leader"          # 持有租约，负责装配订阅/调度并写网关
    FOLLOWER = "follower"      # 待命，等待接管
    ERROR = "error"            # 选举异常（require_redis=True 时会退出）


@dataclass
class ElectorState:
    """选举状态快照，供 /health 与 /-/cluster 输出。"""

    enabled: bool = False
    role: ClusterRole = ClusterRole.STANDALONE
    instance_id: str = ""
    lock_key: str = ""
    lock_ttl_sec: int = 0
    renew_interval_sec: int = 0
    last_renew_ts: float = 0.0
    last_error: str = ""
    redis_ok: bool = False
    failovers: int = 0          # 本副本成功接管次数
    lost_leases: int = 0        # 本副本失去租约次数
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "role": self.role.value,
            "instance_id": self.instance_id,
            "lock_key": self.lock_key,
            "lock_ttl_sec": self.lock_ttl_sec,
            "renew_interval_sec": self.renew_interval_sec,
            "lease_age_sec": round(max(0.0, time.time() - self.last_renew_ts), 1)
            if self.last_renew_ts else None,
            "redis_ok": self.redis_ok,
            "failovers": self.failovers,
            "lost_leases": self.lost_leases,
            "last_error": self.last_error,
            **self.extra,
        }


def _default_instance_id() -> str:
    """生成默认实例标识：``<host>-<pid>-<6位随机>``。"""
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


class LeaderElector:
    """Redis 租约 leader 选举器。

    Args:
        redis_url: Redis 连接串。
        key_prefix: 键前缀。
        lock_ttl_sec: 租约 TTL（秒）。
        renew_interval_sec: 续约周期（秒）。
        instance_id: 本副本标识，留空自动生成。
        redis_timeout_sec: Redis 超时（秒）。
        require_redis: Redis 不可用是否视为致命。
        redis_factory: 可注入的客户端工厂（测试用），签名 ``(url, timeout) -> client``。
        clock: 可注入时钟（测试用）。
        on_role_change: 角色变化回调 ``(old_role, new_role) -> None``，
            仅在这个回调里做「装配订阅 / 停止订阅」等重动作，避免轮询线程里干重活。
    """

    def __init__(
        self,
        redis_url: str,
        *,
        key_prefix: str = "discovery-syncer",
        lock_ttl_sec: int = 30,
        renew_interval_sec: int = 10,
        instance_id: str = "",
        redis_timeout_sec: float = 3.0,
        require_redis: bool = False,
        redis_factory: Optional[Callable[[str, float], Any]] = None,
        clock: Callable[[], float] = time.time,
        on_role_change: Optional[Callable[[ClusterRole, ClusterRole], None]] = None,
    ) -> None:
        self._redis_url = redis_url
        self._key_prefix = key_prefix
        self._ttl_sec = max(5, int(lock_ttl_sec))
        self._renew_sec = max(1, int(renew_interval_sec))
        self._instance_id = instance_id or _default_instance_id()
        self._timeout = redis_timeout_sec
        self._require_redis = require_redis
        self._redis_factory = redis_factory or _default_redis_factory
        self._clock = clock
        self._on_role_change = on_role_change

        self._redis: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._state = ElectorState(
            enabled=True,
            instance_id=self._instance_id,
            lock_key=f"{key_prefix}:leader",
            lock_ttl_sec=self._ttl_sec,
            renew_interval_sec=self._renew_sec,
        )

    # -- 生命周期 -----------------------------------------------------------
    def start(self) -> ElectorState:
        """连接 Redis、尝试抢锁并启动续约线程。

        Returns:
            启动后的状态快照。Redis 不可用且 ``require_redis=False`` 时
            角色降级为 ``STANDALONE``（本副本自认 leader）。
        """
        if not self._connect():
            if self._require_redis:
                self._set_role(ClusterRole.ERROR, "Redis 不可用且 require-redis=true")
                return self.state
            # 降级：单机模式继续服务，行为等同 v3.0.0
            LOG.warning("Redis 不可用，降级为单机模式（本副本自认 leader）")
            with self._lock:
                self._state.role = ClusterRole.STANDALONE
                self._state.extra["degraded"] = True
            self._notify(None, ClusterRole.STANDALONE)
            return self.state

        # 首次抢锁：抢到即 leader，抢不到就是 follower
        acquired = self._acquire()
        with self._lock:
            self._state.role = ClusterRole.LEADER if acquired else ClusterRole.FOLLOWER
            self._state.redis_ok = True
            if acquired:
                self._state.last_renew_ts = self._clock()
        LOG.info("集群选举启动: instance=%s role=%s key=%s",
                 self._instance_id, self._state.role.value, self._state.lock_key)
        self._notify(None, self._state.role)

        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="syncer-leader-elector", daemon=True)
        self._thread.start()
        return self.state

    def stop(self) -> None:
        """停止续约线程并主动释放租约（让其他副本尽快接管）。"""
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self._renew_sec + 2)
        self._thread = None
        if self._redis is not None:
            try:
                if self._state.role == ClusterRole.LEADER:
                    self._redis.eval(_RELEASE_LUA, 1, self._state.lock_key, self._instance_id)
                    LOG.info("已主动释放 leader 租约: %s", self._instance_id)
            except Exception as exc:  # noqa: BLE001 - 停机阶段不再抛
                LOG.warning("释放租约失败（忽略）: %s", exc)
            try:
                self._redis.close()
            except Exception:  # noqa: BLE001
                pass
            self._redis = None
        self._state.redis_ok = False

    # -- 状态 ---------------------------------------------------------------
    @property
    def state(self) -> ElectorState:
        with self._lock:
            return ElectorState(**{**self._state.__dict__})

    @property
    def is_leader(self) -> bool:
        """注意：``STANDALONE`` 也算 leader —— 未启用集群时本副本就该干活。"""
        with self._lock:
            return self._state.role in (ClusterRole.LEADER, ClusterRole.STANDALONE)

    @property
    def role(self) -> ClusterRole:
        with self._lock:
            return self._state.role

    # -- 内部实现 -----------------------------------------------------------
    def _connect(self) -> bool:
        """建立 Redis 连接并 PING 验证。失败返回 False。"""
        try:
            client = self._redis_factory(self._redis_url, self._timeout)
            client.ping()
            self._redis = client
            return True
        except Exception as exc:  # noqa: BLE001 - 连接失败即降级
            with self._lock:
                self._state.last_error = f"{type(exc).__name__}: {exc}"
                self._state.redis_ok = False
            LOG.warning("连接 Redis 失败: %s", exc)
            self._redis = None
            return False

    def _acquire(self) -> bool:
        """尝试抢锁（SET NX PX）。返回是否抢到。"""
        if self._redis is None:
            return False
        try:
            ok = self._redis.set(
                self._state.lock_key, self._instance_id,
                nx=True, px=self._ttl_sec * 1000,
            )
            return bool(ok)
        except Exception as exc:  # noqa: BLE001
            self._mark_redis_error(exc)
            return False

    def _renew(self) -> bool:
        """续约。返回是否仍持有租约。"""
        if self._redis is None:
            return False
        try:
            renewed = self._redis.eval(
                _RENEW_LUA, 1, self._state.lock_key,
                self._instance_id, self._ttl_sec * 1000,
            )
            return bool(renewed)
        except Exception as exc:  # noqa: BLE001
            self._mark_redis_error(exc)
            return False

    def _loop(self) -> None:
        """续约/抢锁主循环。"""
        while not self._stop.wait(self._renew_sec):
            try:
                if self._state.role == ClusterRole.LEADER:
                    if self._renew():
                        with self._lock:
                            self._state.last_renew_ts = self._clock()
                            self._state.redis_ok = True
                            self._state.last_error = ""
                        continue
                    # 失去租约：可能被别的副本接管，转为 follower 并尝试重抢
                    with self._lock:
                        self._state.lost_leases += 1
                    LOG.warning("leader 租约丢失，降级为 follower 并重试抢锁")
                    self._set_role(ClusterRole.FOLLOWER, "renew failed")
                # follower（或刚失去租约）：尝试抢锁
                if self._acquire():
                    with self._lock:
                        self._state.failovers += 1
                        self._state.last_renew_ts = self._clock()
                        self._state.redis_ok = True
                        self._state.last_error = ""
                    LOG.info("接管 leader 成功: %s", self._instance_id)
                    self._set_role(ClusterRole.LEADER, "acquired")
            except Exception as exc:  # noqa: BLE001 - 轮询线程不能死
                LOG.exception("选举循环异常: %s", exc)
                with self._lock:
                    self._state.last_error = f"{type(exc).__name__}: {exc}"

    def _mark_redis_error(self, exc: BaseException) -> None:
        """标记 Redis 异常；若允许降级则切到单机模式。"""
        with self._lock:
            self._state.redis_ok = False
            self._state.last_error = f"{type(exc).__name__}: {exc}"
        LOG.warning("Redis 操作失败: %s", exc)
        if not self._require_redis:
            # 降级为单机：本副本继续干活，避免整个集群因 Redis 抖动全线停摆。
            self._set_role(ClusterRole.STANDALONE, f"redis error: {exc}")

    def _set_role(self, role: ClusterRole, reason: str = "") -> None:
        """切换角色并回调（回调里做订阅装配/停止等重活）。"""
        with self._lock:
            old = self._state.role
            if old == role:
                return
            self._state.role = role
            if reason:
                self._state.last_error = reason
        LOG.info("集群角色切换: %s -> %s (%s)", old.value, role.value, reason)
        self._notify(old, role)

    def _notify(self, old: Optional[ClusterRole], new: ClusterRole) -> None:
        """触发角色变化回调；回调异常不能影响选举线程。"""
        if self._on_role_change is None:
            return
        try:
            self._on_role_change(old, new)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("角色变化回调失败: %s", exc)


def _default_redis_factory(url: str, timeout: float):
    """默认 Redis 客户端工厂（延迟 import，未装 redis 时不影响单机运行）。"""
    import redis  # noqa: PLC0415 - 延迟到真正启用集群才需要

    return redis.Redis.from_url(
        url,
        socket_timeout=timeout,
        socket_connect_timeout=timeout,
        decode_responses=True,
        health_check_interval=30,
    )


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------
_elector: Optional[LeaderElector] = None
_elector_lock = threading.Lock()


def get_elector() -> Optional[LeaderElector]:
    """取得全局选举器；未启用集群时为 None。"""
    return _elector


def init_cluster(cluster_cfg: Any, on_role_change=None) -> Optional[LeaderElector]:
    """按配置初始化并启动选举器。

    Args:
        cluster_cfg: ``app.model.config.ClusterConfig`` 实例。
        on_role_change: 角色变化回调。

    Returns:
        选举器实例；``enabled=False`` 时返回 None（完全不触碰 Redis）。
    """
    global _elector
    with _elector_lock:
        if _elector is not None:
            _elector.stop()
            _elector = None
        if cluster_cfg is None or not getattr(cluster_cfg, "enabled", False):
            LOG.info("集群模式未启用（单副本），不连接 Redis")
            return None
        _elector = LeaderElector(
            cluster_cfg.redis_url,
            key_prefix=cluster_cfg.key_prefix,
            lock_ttl_sec=cluster_cfg.lock_ttl_sec,
            renew_interval_sec=cluster_cfg.renew_interval_sec,
            instance_id=cluster_cfg.instance_id,
            redis_timeout_sec=cluster_cfg.redis_timeout_sec,
            require_redis=cluster_cfg.require_redis,
            on_role_change=on_role_change,
        )
        _elector.start()
        return _elector


def shutdown_cluster() -> None:
    """停止并释放选举器（应用关闭时调用）。"""
    global _elector
    with _elector_lock:
        if _elector is not None:
            _elector.stop()
            _elector = None