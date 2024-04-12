from datetime import datetime, timedelta

from fastapi import APIRouter
from fastapi import Response
from starlette.responses import JSONResponse

from app.model.syncer_model import Jobs
from core.database import db
from core.lib.logger import for_handler

router = APIRouter()

logger = for_handler(__name__)

start = datetime.now()


@router.get('/', status_code=200)
async def index():
    """
    @rtype: str 成功返回 OK
    @summary: 心跳检测
    @return:
    """
    return Response(content="OK")


@router.get('/-/reload')
def reload():
    from app.model.config import settings
    settings.load_config()
    return Response(content="OK")


@router.get("/health")
def health():
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
            result["details"].append(f"syncer: {job.target_id},Not running for more than {job.maximum_interval_sec} sec")
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
    response = JSONResponse(content=result, status_code=status_code)
    return response
