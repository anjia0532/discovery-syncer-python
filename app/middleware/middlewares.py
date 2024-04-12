import time
import traceback

from starlette import status
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from starlette.types import ASGIApp

from core.lib import logger

LOGGER = logger.for_middleware(__name__)


async def log_request(request, call_next):
    start_time = time.time()
    response = await call_next(request)
    end_time = time.time()
    process_time = round(end_time - start_time, 4)
    LOGGER.info(f"URL:{request.url} 耗时：{process_time}s+++++++++++++")
    return response


class LimitUploadSize(BaseHTTPMiddleware):
    """限制上传大小"""

    def __init__(self, app: ASGIApp, max_upload_size: int):
        super().__init__(app)
        self.max_upload_size = max_upload_size

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method == 'POST':
            if 'content-length' not in request.headers:
                return Response(status_code=status.HTTP_411_LENGTH_REQUIRED)
            context_length = int(request.headers['content-length'])
            if context_length > self.max_upload_size:
                return Response(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        return await call_next(request)


class CostTimeHeaderMiddleware(BaseHTTPMiddleware):
    """请求响应耗时"""

    async def dispatch(self, request, call_next):
        start_time = time.time()
        response = await call_next(request)
        end_time = time.time()
        process_time = round(end_time - start_time, 4)
        LOGGER.info(f"URL:{request.url} IP:{request.client.host} 耗时：{process_time}s")
        return response


class AllExceptionHandler(BaseHTTPMiddleware):
    """全局异常拦截"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            responser = await call_next(request)
        except Exception as e:
            LOGGER.error(f"全局异常拦截\nURL:{request.url}\nHeaders:{request.headers}\n{traceback.format_exc()}")
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    "code": 500,
                    "data": {},
                    "message": "服务异常"
                }
            )
        return responser
