"""启动进程内定时调度器。

原先使用 ``funboost_background_scheduler_redis_store`` 并通过
``set_process_jobs_redis_lock_key`` 依赖 Redis 做多副本互斥；现在改为本地
APScheduler（MemoryJobStore）。**单副本约束**见 ``app/runtime/scheduler.py``。
"""

from core.lib import logger
from app.runtime import get_scheduler

LOGGER = logger.for_service('scheduler')


def register_scheduler(app):
    """在应用模块加载时启动本地调度器。"""
    get_scheduler().start()
    LOGGER.info('local scheduler started (single-replica)')
