from app.handler import index, discovery


def register_router(app):

    app.include_router(index.router)
    app.include_router(discovery.router)
