"""One-shot entrypoint поверх DI: собрать агент, задать запрос, получить события.

Упаковывает рутину, которая раньше делалась в каждом caller'е (тесте,
CLI, API-хендлере):

* грузит конфиг через :class:`ConfigLoader`;
* применяет точечные оверрайды полей ``AgentConfig`` (например,
  ``max_iterations=1`` для интеграционных smoke-тестов);
* строит контейнер (логирование настраивается там же);
* открывает per-request scope, бутстрапит user-workspace по id;
* подставляет :class:`CapturingSink` рядом с боевыми sink'ами, чтобы
  caller получил события как ``list[AgentEvent]`` — вся побочка
  (console, history) при этом отрабатывает штатно.

Пример::

    harness = AgentHarness(max_iterations=1)
    events = harness.ask(workspace_id, "привет")

Для тестов оборачивается в pytest-фикстуру; для прод-entrypoint'а
(CLI / HTTP) используется напрямую.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from boba.domain.agent.events import AgentEvent
from boba.domain.agent.meat import Agent
from boba.domain.agent.models import AgentContext, AgentRequest, RequestId
from boba.domain.core.patterns import StreamSink, StreamSinkPipeline, StreamSourceLoop
from boba.domain.core.workspace import ProjectWorkspaceRegistry, WorkspaceId
from boba.infra.config import ConfigLoader
from boba.infra.container import create_container, request_scope
from boba.infra.logging import configure_logging, log_context


class CapturingSink(StreamSink[AgentContext, AgentEvent]):
    """Sink, который копит все события в списке — для ассертов и отладки."""

    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def name(self) -> str:
        return "CapturingSink"

    def handle(self, ctx: AgentContext, event: AgentEvent) -> None:
        del ctx
        self.events.append(event)


class AgentHarness:
    """Собирает контейнер один раз и отдаёт ``ask(ws_id, query)``.

    ``agent_overrides`` — именованные поля :class:`AgentConfig`, которые
    нужно перетереть поверх значений из env/TOML. Типично для тестов
    пишут ``AgentHarness(max_iterations=1)``.
    """

    def __init__(self, **agent_overrides: Any) -> None:
        loader = ConfigLoader()
        self._app_config = loader.load_app()
        # Процессный bootstrap: именно harness владеет глобальными
        # side-effect'ами типа logging. create_container — чистая функция.
        configure_logging(self._app_config.log_level, self._app_config.log_file)
        agent_config = loader.load_agent()
        if agent_overrides:
            agent_config = replace(agent_config, **agent_overrides)
        self._agent_config = agent_config
        self._llm_defaults = loader.load_llm_defaults()
        self._container = create_container(
            self._app_config, self._agent_config, self._llm_defaults
        )

    def ask(self, workspace_id: WorkspaceId, query: str) -> list[AgentEvent]:
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
            capturing = CapturingSink()
            sink = StreamSinkPipeline([real_sink, capturing])
            agent = Agent(source, sink)

            request = AgentRequest(
                query=query,
                model=self._app_config.llm.model,
                workspace_id=shell.workspace_id,
                request_id=request_id,
            )
            agent.run(self._agent_config, request)
            return capturing.events
