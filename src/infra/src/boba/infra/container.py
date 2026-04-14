"""DI-контейнер приложения."""

from __future__ import annotations

from dishka import make_container, Container

from boba.domain.config import AppConfig
from boba.infra.app import AppProvider


def create_container(config: AppConfig) -> Container:
    return make_container(AppProvider(config))  # pyright: ignore[reportArgumentType]
