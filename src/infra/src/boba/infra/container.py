"""DI-контейнер приложения."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from dishka import Container, make_container

from boba.domain.config import AppConfig
from boba.domain.core.workspace import WorkspaceId
from boba.infra.app import AppProvider, RequestProvider


def create_container(config: AppConfig) -> Container:
    return make_container(  # pyright: ignore[reportArgumentType]
        AppProvider(config),
        RequestProvider(),
    )


@contextmanager
def request_scope(
    container: Container, ws_id: WorkspaceId | None = None
) -> Iterator[Container]:
    with container({WorkspaceId | None: ws_id}) as request:
        yield request
