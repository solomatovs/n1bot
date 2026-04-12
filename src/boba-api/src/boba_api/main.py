"""Boba FastAPI — точка входа."""
from __future__ import annotations

from contextlib import asynccontextmanager

from dishka import Container
from fastapi import FastAPI

from boba_api.config import ApiConfig
from boba_infra.container import create_container

from boba_api.routes import chat, health


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.container = create_container()
    yield
    app.state.container.close()


def create_app() -> FastAPI:
    cfg = ApiConfig.from_env()
    prefix = cfg.root_path
    app = FastAPI(title="Boba API", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router, prefix=prefix)
    app.include_router(chat.router, prefix=f"{prefix}/api")
    return app


app = create_app()
