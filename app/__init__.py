import argparse

from fastapi import FastAPI

from app.model.config import settings as app_settings
from core import init_core_modules
# from core.celery.celery import register_celery
from core.lib import logger
from core.settings import settings
from .consume import register_task
from .exception import register_exceptions
from .handler import register_router
from .middleware import register_middlewares
from .scheduler.scheduler import register_scheduler
from .tasks.task_syncer import reload

LOGGER = logger.get('FASTAPI_APP')

"""
FastAPI application main module
The APP instance will be launched by uvicorn instance in ${workspaceFolder}/main.py
"""
FASTAPI_CFG = {
    'env': settings.ENV,
    'title': settings.TITLE,
    'description': settings.DESCRIPTION,
    'version': settings.VERSION,
}
APP = FastAPI(**FASTAPI_CFG)


@APP.on_event('startup')
async def startup_event():
    parser = argparse.ArgumentParser(description="Process some integers.")
    parser.add_argument('--config', '-c', type=str, help='local config path or http url')
    args = parser.parse_args()
    app_settings.config_path = args.config
    reload()
    LOGGER.info(f'config path is : {args.config}')

# 加载核心模块
init_core_modules(APP)
# 注册自定义错误
# register_exceptions(APP)
# 注册中间件
# register_middlewares(APP)

# 注册业务路由
register_router(APP)
# 注册定时任务
register_scheduler(APP)
# 注册消费者，指定任务函数所在模块，任务函数必须以task_开头
# register_task(['app.tasks'])
