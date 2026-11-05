"""本地定时调度器，替代 ``funboost_background_scheduler_redis_store``。

* 使用 APScheduler ``BackgroundScheduler`` + ``MemoryJobStore``，**进程内**。
* 不再需要 Redis 分布式锁（``set_process_jobs_redis_lock_key``）。
* APScheduler 在 :meth:`start` 内延迟导入，因此 ``add_cron`` / ``remove_all``
  的编排逻辑可以在未安装 APScheduler 的环境里做纯逻辑单元测试。

单副本约束
----------
去掉 Redis 锁后必须保证同一时刻只有一个进程在跑定时作业
（K8s ``replicas: 1`` 或自行做 leader 选举）。订阅事件本身也是进程内状态，
因此本服务本来就不适合多副本同时写网关。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Dict, Optional

__all__ = ["LocalScheduler"]

LOG = logging.getLogger("syncer.scheduler")


class LocalScheduler:
    """进程内 cron 调度器。

    Attributes:
        _jobs: ``job_id -> spec`` 的逻辑视图，供测试与 reload 重建使用。
    """

    def __init__(self) -> None:
        self._aps: Optional[Any] = None
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._started = False

    def start(self) -> None:
        """启动 APScheduler，并回放已登记的作业。"""
        if self._started:
            return
        from apscheduler.executors.pool import ThreadPoolExecutor as APThreadPoolExecutor
        from apscheduler.jobstores.memory import MemoryJobStore
        from apscheduler.schedulers.background import BackgroundScheduler

        self._aps = BackgroundScheduler(
            jobstores={"default": MemoryJobStore()},
            executors={"default": APThreadPoolExecutor(8)},
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 30},
            timezone="Asia/Shanghai",
        )
        self._aps.start()
        self._started = True
        for job_id, spec in list(self._jobs.items()):
            self._apply(job_id, spec)
        LOG.info("本地调度器已启动，回放 %s 个作业", len(self._jobs))

    def add_cron(
        self,
        *,
        job_id: str,
        func: Callable[..., None],
        trigger_fields: Optional[Dict[str, Any]] = None,
        next_run_time: Optional[datetime] = None,
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        """登记一个定时作业（``replace_existing=True`` 语义）。

        Args:
            job_id: 作业 ID，重复登记会覆盖。
            func: 到点执行的函数。
            trigger_fields: ``CronTrigger`` 的 6 个字段。
            next_run_time: 若非空则改为一次性 ``date`` 触发（兼容 ``@reboot``）。
            kwargs: 传给 func 的关键字参数。
        """
        spec = {
            "func": func,
            "trigger_fields": trigger_fields,
            "next_run_time": next_run_time,
            "kwargs": kwargs or {},
        }
        self._jobs[job_id] = spec
        if self._started:
            self._apply(job_id, spec)

    def _apply(self, job_id: str, spec: Dict[str, Any]) -> None:
        """把逻辑作业真正注册到 APScheduler。"""
        if self._aps is None:
            return
        common = {
            "id": job_id, "name": job_id, "kwargs": spec["kwargs"],
            "replace_existing": True, "coalesce": True, "max_instances": 1,
        }
        if spec["next_run_time"] is not None:
            self._aps.add_job(spec["func"], "date", run_date=spec["next_run_time"], **common)
        else:
            from apscheduler.triggers.cron import CronTrigger

            self._aps.add_job(spec["func"], CronTrigger(**(spec["trigger_fields"] or {})), **common)

    def remove_all(self) -> None:
        """清空全部作业（``/-/reload`` 重建时调用）。"""
        self._jobs.clear()
        if self._started and self._aps is not None:
            self._aps.remove_all_jobs()

    def shutdown(self, wait: bool = False) -> None:
        """停止调度器。"""
        if self._aps is not None:
            self._aps.shutdown(wait=wait)
        self._started = False
        self._aps = None

    @property
    def job_ids(self) -> list:
        """当前登记的逻辑作业 ID 列表（测试/运维用）。"""
        return sorted(self._jobs)

    @property
    def started(self) -> bool:
        return self._started
