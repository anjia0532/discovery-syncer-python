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
        from app.runtime import get_task_bus
        from app.tasks.task_syncer import reload

        settings.load_config()
        # 配置加载与订阅重建解耦：提交到本地任务总线异步执行，保持接口快速返回。
        get_task_bus().submit(reload)
    except Exception as e:
        logger.error(f"重新加载配置文件报错", exc_info=e)
    return Response(content=RESP_OK)


@router.get('/show-memory', summary="显示内存占用最大的前N个对象", description="显示内存占用最大的前N个对象")
def show_memory(num: Annotated[int, Query(title="num", description="前几，默认20")] = 20):
    """
    显示内存占用最大的前N个对象
    @rtype: str
    @return: 成功返回 OK

    注意：``gc.get_objects()`` 会遍历整个堆并在调用期间持有 GIL，
    属于**诊断接口**，不要放进常规探活路径，也不要在高 QPS 下调用。
    这里用 ``heapq.nlargest`` 流式取 top-N，避免像旧实现那样
    把所有对象和大小都堆进列表再排序（内存翻倍）。
    """
    import heapq

    num = max(1, min(num, 200))  # 限制上限，避免一次输出几百 MB
    top = heapq.nlargest(
        num,
        ((sys.getsizeof(obj), id(obj), obj) for obj in gc.get_objects()),
        key=lambda item: item[0],
    )
    contents = [
        f"OBJ: {oid}, TYPE:{type(obj)}, SIZE: {size / 1024 / 1024:.2f}MB, REPR: {str(obj)[:100]}"
        for size, oid, obj in top
    ]
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
        # 订阅模式下不能只看 Jobs.last_time：旧实现只在「有 diff」时才刷新它，
        # 事件驱动/无变更期间会被误判为失联。这里合并订阅心跳作为最后活跃时间。
        from app.tasks.task_syncer import get_manager

        manager = get_manager()
        result = {
            "total": len(jobs),
            "running": 0,
            "lost": 0,
            "details": [],
            "status": "UNKNOWN",
            "uptime": timedelta(seconds=(current - start).total_seconds()).__str__(),
            "subscription": {
                tid: {"mode": s["mode"], "version": s["version"],
                      "alive": s["alive"], "degraded": s["degraded"]}
                for tid, s in manager.all_status().items()
            },
        }
        default_last_time = current - timedelta(days=365)
        for job in jobs:
            heartbeat_ts = manager.last_heartbeat(job.target_id)
            heartbeat_dt = datetime.fromtimestamp(heartbeat_ts) if heartbeat_ts else None
            candidates = [t for t in (job.last_time, heartbeat_dt) if t is not None]
            last_active = max(candidates) if candidates else default_last_time
            diff = (current - last_active).total_seconds()
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

        # 集群模式：follower 本来就不跑同步作业，jobs 数为 0 或心跳缺失是正常的，
        # 不能因为「没有作业在跑」就判 DOWN，否则 K8s 会把待命副本反复重启。
        from app.runtime import get_elector

        elector = get_elector()
        if elector is not None:
            cstate = elector.state.to_dict()
            result["cluster"] = cstate
            role = cstate["role"]
            if role == "follower":
                # 待命副本：只要 Redis 连通就算健康
                result["status"] = "UP" if cstate["redis_ok"] else "WARN"
                status_code = 200
            elif role == "error":
                result["status"] = "DOWN"
                status_code = 500
    except Exception as e:
        result = {"status": "ERROR", "details": []}
        status_code = 500
        logger.error(f"健康检查报错", exc_info=e)
    response = JSONResponse(content=result, status_code=status_code)
    return response
