from apscheduler.triggers.cron import CronTrigger
from funboost import funboost_aps_scheduler

from core.lib import logger

LOGGER = logger.for_service('scheduler')


def register_scheduler(app):
    funboost_aps_scheduler.start()
