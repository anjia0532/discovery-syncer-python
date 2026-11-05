"""本地运行时：进程内任务总线 + 进程内定时调度器 + 可选集群选举。

单副本部署（默认）只用 TaskBus + LocalScheduler，完全不依赖 Redis。
多副本部署时通过 :mod:`app.runtime.cluster` 的 leader 选举决定由谁装配订阅、
跑定时作业并写网关，其余副本待命自动接管。

全局单例通过 :func:`get_task_bus` / :func:`get_scheduler` / :func:`get_elector`
获取，:func:`shutdown_runtime` 在应用关闭时统一释放。
"""

from __future__ import annotations

from threading import Lock
from typing import Optional

from .cluster import (
    ClusterRole,
    LeaderElector,
    get_elector,
    init_cluster,
    shutdown_cluster,
)
from .scheduler import LocalScheduler
from .taskbus import TaskBus

__all__ = [
    "TaskBus", "LocalScheduler", "get_task_bus", "get_scheduler", "shutdown_runtime",
    "ClusterRole", "LeaderElector", "get_elector", "init_cluster", "shutdown_cluster",
]

_lock = Lock()
_task_bus: Optional[TaskBus] = None
_scheduler: Optional[LocalScheduler] = None


def get_task_bus() -> TaskBus:
    """取得（或惰性创建）全局任务总线。"""
    global _task_bus
    with _lock:
        if _task_bus is None:
            _task_bus = TaskBus()
        return _task_bus


def get_scheduler() -> LocalScheduler:
    """取得（或惰性创建）全局本地调度器。"""
    global _scheduler
    with _lock:
        if _scheduler is None:
            _scheduler = LocalScheduler()
        return _scheduler


def shutdown_runtime() -> None:
    """释放运行时资源（应用 shutdown 时调用）。"""
    global _task_bus, _scheduler
    # 先放掉 leader 租约，让其他副本尽快接管，再停调度与线程池。
    shutdown_cluster()
    with _lock:
        bus, scheduler = _task_bus, _scheduler
        _task_bus, _scheduler = None, None
    if scheduler is not None:
        scheduler.shutdown()
    if bus is not None:
        bus.shutdown()
