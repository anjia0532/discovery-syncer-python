"""进程内任务总线，替代 funboost 的 ``@boost`` / ``publish`` / ``consume``。

设计取舍
--------
* 用 ``ThreadPoolExecutor`` 而非 Redis Stream：本服务是单副本状态机
  （内存客户端字典、内存订阅管理器、UDP socket、SQLite 任务库），
  跨进程分发没有意义。
* 保留「重试」这一唯一有实际价值的队列增强；死信/分布式控频/消息过期
  全部去掉，由 5 分钟全量对账兜底与 httpx 超时覆盖。
* ``contextvars`` 通过 ``copy_context`` 传播到工作线程，使日志里的
  ``task_id`` 能串起父任务与子任务。
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable, Dict, Optional

from core.lib.task_context import TaskContext, bind_task, new_task_id, reset_task

__all__ = ["TaskBusStats", "TaskBus"]

LOG = logging.getLogger("syncer.taskbus")


@dataclass
class TaskBusStats:
    """任务总线统计，用于替代 funboost 死信队列的可观测性。"""

    submitted: int = 0
    succeeded: int = 0
    failed: int = 0
    retries: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "submitted": self.submitted, "succeeded": self.succeeded,
            "failed": self.failed, "retries": self.retries,
        }


class TaskBus:
    """本地任务执行器。

    Args:
        max_workers: 工作线程数（原 funboost 各队列 qps 之和约 300，这里用固定线程池即可）。
        max_retries: 单任务最大重试次数（原 ``max_retry_times=4``，这里收敛为 2）。
        retry_interval: 重试基础间隔秒数，按次数线性退避。
        sleep: 可注入的休眠函数，便于测试不真实等待。
    """

    def __init__(
        self,
        max_workers: int = 16,
        max_retries: int = 2,
        retry_interval: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="syncer-task")
        self._max_retries = max_retries
        self._retry_interval = retry_interval
        self._sleep = sleep
        self._lock = Lock()
        self._stats = TaskBusStats()
        self._closed = False

    def submit(self, fn: Callable[..., None], *, task_id: Optional[str] = None, **kwargs: Any) -> Future:
        """异步提交任务。

        Args:
            fn: 要执行的函数。
            task_id: 可选，父任务传入时用于串联日志；不传则自动生成。
            **kwargs: 原样传给 ``fn``。

        Returns:
            ``Future``；任务内部已捕获异常并记日志，Future 不会抛出。
        """
        tid = task_id or new_task_id()
        with self._lock:
            if self._closed:
                raise RuntimeError("TaskBus 已关闭，无法提交任务")
            self._stats.submitted += 1
        ctx = copy_context()

        def _run() -> Optional[Exception]:
            token = bind_task(TaskContext(task_id=tid, submit_time=time.time()))
            try:
                for attempt in range(self._max_retries + 1):
                    try:
                        fn(**kwargs)
                        with self._lock:
                            self._stats.succeeded += 1
                        return None
                    except Exception as exc:  # noqa: BLE001 - 单任务失败不能拖垮线程池
                        if attempt >= self._max_retries:
                            with self._lock:
                                self._stats.failed += 1
                            LOG.exception("任务最终失败 task_id=%s fn=%s", tid, getattr(fn, "__name__", fn))
                            return exc
                        with self._lock:
                            self._stats.retries += 1
                        LOG.warning("任务失败，准备重试 task_id=%s attempt=%s err=%s", tid, attempt + 1, exc)
                        self._sleep(self._retry_interval * (attempt + 1))
                return None
            finally:
                reset_task(token)

        return self._pool.submit(ctx.run, _run)

    @property
    def stats(self) -> Dict[str, int]:
        with self._lock:
            return self._stats.to_dict()

    def shutdown(self, wait: bool = False) -> None:
        """关闭线程池；默认不等待正在跑的任务，避免停机被长 HTTP 请求卡住。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._pool.shutdown(wait=wait, cancel_futures=not wait)
