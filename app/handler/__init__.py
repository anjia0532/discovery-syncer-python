RESP_OK = "OK"


def register_router(app):
    from app.handler import discovery, index, notify
    app.include_router(index.router)
    app.include_router(discovery.router)
    app.include_router(notify.router)
