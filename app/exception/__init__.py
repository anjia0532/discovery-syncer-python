from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.exception.exception_handler import validation_exception_handler, unicorn_exception_handler
from app.exception.exceptions import ParamError, ExceptionError, FileNotFound


def register_exceptions(app: FastAPI):
    # 只能拦截指定的错误类型，不能拦截子类
    app.exception_handler(RequestValidationError)(validation_exception_handler)
    app.exception_handler(ExceptionError)(unicorn_exception_handler)
    app.exception_handler(ParamError)(unicorn_exception_handler)
    app.exception_handler(FileNotFound)(unicorn_exception_handler)

    # app.add_exception_handler()
