"""Сборка контейнера и запуск одного запроса с внешним sink'ом.

Повторяет логику :class:`boba.infra.AgentHarness`, но разрешает подставить
дополнительный :class:`StreamSink` (чтобы UI получал события параллельно
штатному ``ConsoleSink`` из DI). Harness не модифицируем — web-слой
отдельный и не должен тянуть на себя изменения в infra.

Контейнер строится один раз на процесс Chainlit: DI-стоимость высокая,
а состояние между сессиями (``WorkspaceId``) живёт снаружи — в
``cl.user_session``.
"""

from __future__ import annotations

from boba.domain.agent.events import AgentEvent
from boba.domain.agent.meat import Agent, AgentContext
from boba.domain.agent.models import AgentRequest, RequestId
from boba.domain.core.patterns import (
    StreamSink,
    StreamSinkPipeline,
    StreamSourceLoop,
)
from boba.domain.core.workspace import (
    ProjectWorkspaceRegistry,
    ProjectWorkspaceShell,
    WorkspaceId,
)
from boba.infra.config import ConfigLoader
from boba.infra.container import create_container, request_scope
from boba.infra.logging import configure_logging, log_context


class ChatSession:
    """Инкапсулирует DI-контейнер и per-query запуск агента.

    Один инстанс на процесс Chainlit. ``run`` открывает per-request scope,
    подмешивает ``extra_sink`` в pipeline со штатным DI-sink'ом и блокирует
    вызывающий поток до завершения цикла агента. Async-handler Chainlit
    вызывает ``run`` через ``asyncio.to_thread``.
    """

    def __init__(self) -> None:
        loader = ConfigLoader()
        self._app_config = loader.load_app()
        configure_logging(self._app_config.log_level, self._app_config.log_file)
        self._agent_config = loader.load_agent()
        self._llm_defaults = loader.load_llm_defaults()
        self._container = create_container(
            self._app_config, self._agent_config, self._llm_defaults
        )

    def project_workspace(
        self, workspace_id: WorkspaceId
    ) -> ProjectWorkspaceShell:
        """Project-workspace пользователя: тот же, куда смотрят file-tools агента.

        Реестр ``ProjectWorkspaceRegistry`` живёт в APP scope (см.
        :class:`boba.infra.app.AppProvider`), поэтому shell доступен вне
        request scope — чат-UI использует его для upload/list/delete
        независимо от прогона агента.
        """
        registry = self._container.get(ProjectWorkspaceRegistry)
        shell = registry.get_or_create(workspace_id)
        if not isinstance(shell, ProjectWorkspaceShell):
            msg = (
                f"ProjectWorkspaceRegistry returned "
                f"{type(shell).__name__}, expected ProjectWorkspaceShell"
            )
            raise TypeError(msg)
        return shell

    def run(
        self,
        workspace_id: WorkspaceId,
        query: str,
        extra_sink: StreamSink[AgentContext, AgentEvent],
        *,
        model: str,
    ) -> None:
        """Запустить агентский цикл. ``model`` обязателен и определяется
        только на стороне UI (ChatSettings) — конфиг в агентский луп не
        просачивается.
        """
        registry = self._container.get(ProjectWorkspaceRegistry)
        shell = registry.get_or_create(workspace_id)
        request_id = RequestId.new()

        with (
            log_context(
                request_id=request_id.to_wire(),
                workspace_id=shell.workspace_id.to_wire(),
            ),
            request_scope(self._container, shell.workspace_id) as req,
        ):
            source = req.get(StreamSourceLoop[AgentContext, AgentEvent])
            real_sink = req.get(StreamSink[AgentContext, AgentEvent])
            sink = StreamSinkPipeline([real_sink, extra_sink])
            agent = Agent(source, sink)

            request = AgentRequest(
                query=query,
                model=model,
                workspace_id=shell.workspace_id,
                request_id=request_id,
            )
            agent.run(self._agent_config, request)
