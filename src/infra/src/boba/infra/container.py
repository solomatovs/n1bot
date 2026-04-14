"""DI-контейнер приложения."""

from __future__ import annotations

from dishka import make_container, Container

from boba.domain.config import AppConfig
from boba.infra.app import AppProvider, WorkspaceProvider


def create_container(config: AppConfig) -> Container:
    return make_container(  # pyright: ignore[reportArgumentType]
        AppProvider(config),
        WorkspaceProvider(),
    )
