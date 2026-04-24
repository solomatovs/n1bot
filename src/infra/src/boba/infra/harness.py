"""Тестовый harness: собрать агент, задать запрос, получить события списком.

:class:`CapturingSink` — sink, который копит все события в списке.
:class:`AgentHarness` — обёртка над :func:`create_agent_source`:
подхватывает :class:`ConfigLoader`/:class:`SamplingLoader`,
настраивает логирование, даёт метод ``ask(query, model=...)``.

В отличие от старой версии harness'а:

- без Dishka-контейнера и request_scope — их в ``boba`` нет;
- без ``workspace_id`` — :class:`AgentRequest` в ``boba`` его
  больше не хранит;
- без двойного sink'а (``real_sink + CapturingSink``) — тесты
  не хотят боевую побочку вроде ``TextOutSink``; получают только
  список событий.

Используется только тестами (:mod:`tests/conftest.py`) — для
продового запуска есть :mod:`boba.app`.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from boba.adapters.in_memory_messages import InMemoryMessageService
from boba.domain.agent.events import AgentEvent
from boba.domain.agent.meat.agent import Agent
from boba.domain.agent.models import AgentContext, AgentRequest
from boba.domain.core.patterns import StreamSink
from boba.domain.llm.models import RequestId
from boba.infra.config import ConfigLoader, SamplingLoader
from boba.infra.container import (
    AgentComponents,
    create_agent_source,
    create_empty_tools_service,
    create_llm_source,
    default_static_prompt_providers,
)
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
    """One-shot обёртка над :func:`create_agent_source` для тестов.

    ``agent_overrides`` — именованные поля :class:`AgentConfig`, которые
    перетираются поверх значений из env/TOML. Типично для тестов:
    ``AgentHarness(max_iterations=1)``.
    """

    _DEFAULT_SYSTEM_PROMPT = "you are a helpful assistant. Answer concisely"

    def __init__(self, **agent_overrides: Any) -> None:
        loader = ConfigLoader()
        self._app_config = loader.load_app()
        # Процессный bootstrap: именно harness владеет глобальными
        # side-effect'ами типа logging.
        configure_logging(self._app_config.log_level, self._app_config.log_file)
        agent_config = loader.load_agent()
        if agent_overrides:
            agent_config = replace(agent_config, **agent_overrides)
        self._agent_config = agent_config
        self._sampling = SamplingLoader().load()

    def ask(self, query: str, *, model: str) -> list[AgentEvent]:
        """Выполнить один запрос. ``model`` обязателен — имя модели не
        живёт в конфиге, caller передаёт его явно.
        """
        request_id = RequestId.new()
        llm_source = create_llm_source(self._app_config.llm)
        source = create_agent_source(
            llm_source,
            AgentComponents(
                agent_config=self._agent_config,
                sampling=self._sampling,
                prompt_providers=default_static_prompt_providers(
                    system_prompt=self._DEFAULT_SYSTEM_PROMPT,
                ),
                message_service=InMemoryMessageService(),
                tools_service=create_empty_tools_service(),
            ),
        )
        capturing = CapturingSink()
        agent = Agent(source=source, sink=capturing)
        request = AgentRequest(
            query=query,
            model=model,
            request_id=request_id,
        )
        with log_context(request_id=request_id.to_wire()):
            agent.run(self._agent_config, request)
        return capturing.events
