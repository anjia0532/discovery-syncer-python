RESP_OK = "OK"


def register_router(app):
    from app.handler import index, discovery
    app.include_router(index.router)
    app.include_router(discovery.router)
