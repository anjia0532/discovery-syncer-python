import gc
import sys
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter
from fastapi import Response
from fastapi.params import Query
from starlette.responses import JSONResponse

from app.model.syncer_model import Jobs
from core.database import db
from core.lib.logger import for_handler
from . import RESP_OK

router = APIRouter()

logger = for_handler(__name__)

start = datetime.now()


@router.get('/', status_code=200, summary="心跳检测", description="心跳检测")
async def index():
    """
    心跳检测
    @rtype: str
    @return: 成功返回 OK
    """
    return Response(content=RESP_OK)


@router.get('/-/reload', summary="重新加载配置文件", description="重新加载配置文件")
def reload():
    """
    重新加载配置文件
    @rtype: str
    @return: 成功返回 OK
    """
    try:
        from app.model.config import settings
        settings.load_config()
    except Exception as e:
        logger.error(f"重新加载配置文件报错", exc_info=e)
    return Response(content=RESP_OK)


@router.get('/show-memory', summary="显示内存占用最大的前N个对象", description="显示内存占用最大的前N个对象")
def show_memory(num: Annotated[int, Query(title="num", description="前几，默认20")] = 20):
    """
    显示内存占用最大的前N个对象
    @rtype: str
    @return: 成功返回 OK
    """
    objects_list = []
    contents = []
    for obj in gc.get_objects():
        size = sys.getsizeof(obj)
        objects_list.append((obj, size))
    sorted_values = sorted(objects_list, key=lambda x: x[1], reverse=True)
    for obj, size in sorted_values[:num]:
        contents.append(f"OBJ: {id(obj)}, TYPE:{type(obj)}, SIZE: {size / 1024 / 1024:.2f}MB, REPR: {str(obj)[:100]}")
    return Response(content="\n".join(contents))


@router.get("/health", summary="健康检查", description="健康检查")
def health():
    """
    健康检查
    @rtype: json
    @return: 成功返回 OK
    """
    try:
        enginex, sqla_helper = db.get_sqla_helper()
        jobs = Jobs.query_all(sqla_helper)
        current = datetime.now()
        result = {
            "total": len(jobs),
            "running": 0,
            "lost": 0,
            "details": [],
            "status": "UNKNOWN",
            "uptime": timedelta(seconds=(current - start).total_seconds()).__str__()
        }
        default_last_time = current - timedelta(days=365)
        for job in jobs:
            diff = (current - (job.last_time or default_last_time)).total_seconds()
            if 0 < job.maximum_interval_sec < diff:
                result["lost"] += 1
                result["details"].append(
                    f"syncer: {job.target_id},Not running for more than {job.maximum_interval_sec} sec")
            else:
                result["running"] += 1
                result["details"].append(f"syncer: {job.target_id},is ok")
        status_code = 200
        if result["running"] == len(jobs):
            result["status"] = "UP"
        elif result["running"] == 0 and result["lost"] > 0:
            status_code = 500
            result["status"] = "DOWN"
        elif result["running"] > 0 and result["lost"] > 0:
            result["status"] = "WARN"
    except Exception as e:
        result = {"status": "ERROR", "details": []}
        status_code = 500
        logger.error(f"健康检查报错", exc_info=e)
    response = JSONResponse(content=result, status_code=status_code)
    return response
