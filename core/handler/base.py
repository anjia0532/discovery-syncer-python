from fastapi import APIRouter

from ..lib import logger

LOGGER = logger.for_handler('base')

ROUTER = APIRouter()

