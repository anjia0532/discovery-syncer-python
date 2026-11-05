import argparse

from fastapi import FastAPI

from app.model.config import settings as app_settings
from core import init_core_modules
# from core.celery.celery import register_celery
from core.lib import logger
from core.settings import settings
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
# 注意：FastAPI 没有 env 参数，原先把 settings.ENV 传进去会直接 TypeError
# 导致应用无法启动（真实启动验证才发现）。这里只传 FastAPI 支持的字段。
FASTAPI_CFG = {
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


@APP.on_event('shutdown')
async def shutdown_event():
    """优雅停机：释放 leader 租约、停止订阅线程、释放 UDP 端口与本地线程池/调度器。"""
    from app.runtime import shutdown_runtime
    from app.tasks.task_syncer import get_manager

    # 先停订阅（可能持有 UDP/gRPC 连接），再放租约并关运行时。
    # shutdown_runtime 内部会 shutdown_cluster() 主动释放 leader 租约，
    # 让其他副本尽快接管，而不是等 TTL 自然过期。
    get_manager().stop_all()
    shutdown_runtime()
    LOGGER.info('subscription manager and local runtime stopped')

# 加载核心模块
init_core_modules(APP)
# 注册自定义错误
# register_exceptions(APP)
# 注册中间件
register_middlewares(APP)

# 注册业务路由
register_router(APP)
# 注册定时任务
register_scheduler(APP)
