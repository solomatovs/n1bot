"""AgentBuilder — composition root pipeline-слоя агента.

Тонкий fluent-фасад над onion-цепочкой mandatory middleware вокруг
произвольного terminal: StreamSource[AgentContext, AgentEvent].
Сам terminal (типично LLMPort(llm, turn)), HistoryService и
ToolExecutor создаются и принадлежат caller'у — AgentBuilder их
получает уже готовыми и оборачивает в pipeline.

Mandatory-цепочка (outer -> inner):

    HistoryRecorderMiddleware(history)
        -> EventStamperMiddleware
            -> AgentErrorRouterMiddleware(error_router)
                -> [user middleware в порядке регистрации]
                    -> ToolExecutionMiddleware(tool_executor)
                        -> UserQueryRecorderMiddleware
                            -> terminal

tool_executor — обязателен (без него terminal не сможет получать
исполнение tool_call'ов из middleware). История — по умолчанию
InMemoryHistoryService(). error_router — по умолчанию свежий
AgentErrorRouter(). User middleware регистрируется как
Callable[[inner], MW], caller сам захватывает зависимости.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Self

from boba.agent.agent import Agent, AgentContext
from boba.agent.events import AgentEvent
from boba.agent.history import HistoryService, InMemoryHistoryService
from boba.agent.loop import AgentLoop
from boba.agent.middleware import (
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
    EventStamperMiddleware,
    HistoryRecorderMiddleware,
    StopIfContentFilter,
    StopIfLengthReached,
    StopIfReasonStop,
    StopOnAnyFailure,
    ToolExecutionMiddleware,
    UserQueryRecorderMiddleware,
)
from boba.patterns import (
    Specification,
    StreamSource,
)
from boba.tools.framework import ToolExecutor

__all__ = ["AgentBuilder"]

_PipelineStage = StreamSource[AgentContext, AgentEvent]
_MiddlewareFactory = Callable[[_PipelineStage], _PipelineStage]
_StopSpec = Specification[tuple[AgentContext, AgentEvent]]


_DEFAULT_STOPS: tuple[_StopSpec, ...] = (
    StopIfReasonStop(),
    StopIfLengthReached(),
    StopIfContentFilter(),
    StopOnAnyFailure(),
)
"""Дефолтные стоп-условия. Применяются всегда поверх пользовательских."""


class AgentBuilder:
    """Fluent-фасад: shared resources + user middleware + stop conditions.

    build(terminal) оборачивает terminal mandatory-цепочкой и возвращает
    Agent. Terminal (типично LLMPort(llm, turn)) caller собирает сам.
    """

    def __init__(self) -> None:
        self._history: HistoryService = InMemoryHistoryService()
        self._error_router: AgentErrorRouter = AgentErrorRouter()
        self._tool_executor: ToolExecutor | None = None
        self._middlewares: list[_MiddlewareFactory] = []
        self._extra_stops: list[_StopSpec] = []

    # ---- shared resources ------------------------------------------------ #

    def use_history(self, service: HistoryService) -> Self:
        """Подменить HistoryService. Default — InMemoryHistoryService()."""
        self._history = service
        return self

    def use_error_router(self, router: AgentErrorRouter) -> Self:
        """Подменить AgentErrorRouter. Default — свежий AgentErrorRouter()."""
        self._error_router = router
        return self

    def use_tool_executor(self, executor: ToolExecutor) -> Self:
        """Передать готовый ToolExecutor. Обязателен до build()."""
        self._tool_executor = executor
        return self

    # ---- user middleware ------------------------------------------------- #

    def use_middleware(self, factory: _MiddlewareFactory) -> Self:
        """Добавить middleware-фабрику (inner) -> MW.

        Фабрика принимает inner-стрим и возвращает обёртку. Все её
        зависимости caller захватывает через closure.
        """
        self._middlewares.append(factory)
        return self

    # ---- stop conditions ------------------------------------------------- #

    def stop_if(self, spec: _StopSpec) -> Self:
        """Добавить пользовательское stop-условие (additive поверх дефолтов)."""
        self._extra_stops.append(spec)
        return self

    # ---- build ----------------------------------------------------------- #

    def build(self, terminal: _PipelineStage) -> Agent:
        """Собрать pipeline вокруг terminal и вернуть Agent."""
        if self._tool_executor is None:
            msg = (
                "AgentBuilder.build: ToolExecutor не задан. "
                "Передай готовый executor через `use_tool_executor(...)` "
                "(типично — `tool_registry.executor()` из `ToolBuilder`)."
            )
            raise RuntimeError(msg)

        chain: _PipelineStage = terminal
        chain = UserQueryRecorderMiddleware(chain)
        chain = ToolExecutionMiddleware(chain, self._tool_executor)
        for factory in reversed(self._middlewares):
            chain = factory(chain)
        chain = AgentErrorRouterMiddleware(chain, self._error_router)
        chain = EventStamperMiddleware(chain)
        chain = HistoryRecorderMiddleware(chain, self._history)

        source = AgentLoop(source=chain, stop_if=self._build_stop_spec())
        return Agent(source=source)

    # ---- internals ------------------------------------------------------- #

    def _build_stop_spec(self) -> _StopSpec:
        spec: _StopSpec = _DEFAULT_STOPS[0]
        for s in _DEFAULT_STOPS[1:]:
            spec = spec.or_(s)
        for s in self._extra_stops:
            spec = spec.or_(s)
        return spec
