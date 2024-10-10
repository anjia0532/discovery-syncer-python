from funboost.timing_job.apscheduler_use_redis_store import funboost_background_scheduler_redis_store

from core.lib import logger

LOGGER = logger.for_service('scheduler')


def register_scheduler(app):
    funboost_background_scheduler_redis_store.start()
