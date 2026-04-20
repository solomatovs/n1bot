"""DI-контейнер приложения."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from dishka import Container, make_container

from boba.app.logging import configure_logging
from boba.domain.agent.models import AgentConfig, LLMRequestDefaults
from boba.domain.config import AppConfig
from boba.domain.core.workspace import WorkspaceId
from boba.infra.app import AppProvider, RequestProvider


def create_container(
    app_config: AppConfig,
    agent_config: AgentConfig,
    llm_defaults: LLMRequestDefaults,
) -> Container:
    # Логирование — инфраструктурная забота: раньше каждый caller звал
    # configure_logging руками, теперь это делает контейнер.
    # basicConfig(force=True) делает вызов идемпотентным.
    configure_logging(app_config.log_level)
    return make_container(  # pyright: ignore[reportArgumentType]
        AppProvider(app_config, agent_config, llm_defaults),
        RequestProvider(),
    )


@contextmanager
def request_scope(
    container: Container, ws_id: WorkspaceId
) -> Iterator[Container]:
    """Открыть per-request scope.

    ``ws_id`` обязателен и задаёт сессию: все три менеджера
    (user/system/tmp) разделяют один и тот же id через
    ``manager.get_or_create(ws_id)``. Сгенерировать новый id заранее —
    ``WorkspaceId.new()``.
    """
    with container({WorkspaceId: ws_id}) as request:
        yield request
