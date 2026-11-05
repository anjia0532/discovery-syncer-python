"""本地运行时：进程内任务总线 + 进程内定时调度器。

完全替代 funboost + Redis。全局单例通过 :func:`get_task_bus` /
:func:`get_scheduler` 获取，:func:`shutdown_runtime` 在应用关闭时统一释放。
"""

from __future__ import annotations

from threading import Lock
from typing import Optional

from .scheduler import LocalScheduler
from .taskbus import TaskBus

__all__ = ["TaskBus", "LocalScheduler", "get_task_bus", "get_scheduler", "shutdown_runtime"]

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
    with _lock:
        bus, scheduler = _task_bus, _scheduler
        _task_bus, _scheduler = None, None
    if scheduler is not None:
        scheduler.shutdown()
    if bus is not None:
        bus.shutdown()
