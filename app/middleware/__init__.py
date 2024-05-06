from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from .middlewares import LimitUploadSize, CostTimeHeaderMiddleware, log_request, AllExceptionHandler, \
    SyncerApiKeyMiddleware



def register_middlewares(app: FastAPI):
    """早注册的在内层，晚注册的在外层"""
    # app.middleware('http')(log_request)

    # app.add_middleware(LimitUploadSize, max_upload_size=10000000)
    app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(CostTimeHeaderMiddleware)
    app.add_middleware(AllExceptionHandler)
    app.add_middleware(SyncerApiKeyMiddleware)
