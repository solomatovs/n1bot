"""DI-контейнер приложения."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator
from uuid import UUID

from dishka import make_container, Container

from boba.domain.config import AppConfig
from boba.infra.app import AppProvider, RequestProvider


def create_container(config: AppConfig) -> Container:
    return make_container(  # pyright: ignore[reportArgumentType]
        AppProvider(config),
        RequestProvider(),
    )


@contextmanager
def request_scope(
    container: Container, ws_id: UUID | None = None
) -> Iterator[Container]:
    with container({UUID | None: ws_id}) as request:
        yield request
