from fastapi import Request
from fastapi.exceptions import RequestValidationError
from starlette import status
from starlette.responses import JSONResponse

from core.lib import logger
from .exceptions import ExceptionError
from core.model.handler import Resp


LOGGER = logger.get('exception_handler')


def unicorn_exception_handler(request: Request, exc: ExceptionError):
    return JSONResponse(Resp.err(
        message=exc.message, code=exc.code
    ))


def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    捕获请求参数 验证错误
    """
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "code": 400,
            "data": {},
            "detail": exc.errors(),
            "body": exc.body,
            "message": "参数异常"
        }
    )
