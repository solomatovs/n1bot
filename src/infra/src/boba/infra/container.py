"""DI-контейнер приложения."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from dishka import Container, make_container

from boba.domain.agent.models import AgentConfig
from boba.domain.config import AppConfig
from boba.domain.core.workspace import WorkspaceId
from boba.infra.app import AppProvider, RequestProvider


def create_container(
    app_config: AppConfig, agent_config: AgentConfig
) -> Container:
    return make_container(  # pyright: ignore[reportArgumentType]
        AppProvider(app_config, agent_config),
        RequestProvider(),
    )


@contextmanager
def request_scope(
    container: Container, ws_id: WorkspaceId | None = None
) -> Iterator[Container]:
    with container({WorkspaceId | None: ws_id}) as request:
        yield request
