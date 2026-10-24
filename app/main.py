"""The FastAPI app: wires the routers of app/web together. Run with `uvicorn --factory app.main:create_app`."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .db import connect, get_setting, init_db
from .web import admin, checkin, live, public, referee, results
from .web.common import BASE, get_db  # noqa: F401  (get_db: tests override it)


def create_app() -> FastAPI:
    init_db()
    db = connect()
    secret = get_setting(db, "secret")
    db.close()

    app = FastAPI(title="Beer Pong", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret, same_site="lax", max_age=60 * 60 * 24 * 2)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    for module in (public, live, checkin, results, referee):
        app.include_router(module.router)
    app.include_router(admin.admin)
    return app
