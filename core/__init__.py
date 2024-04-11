"""
Core module initialization
"""
from fastapi import FastAPI

from .handler import base


def init_core_modules(app: FastAPI) -> None:
    """
    Init core handlers, middlewares and services for fastapi application
    :return: None
    """
    # handlers
    app.include_router(base.ROUTER)
    # services

