"""LLM — тонкий оркестратор LLM-цепочки + LLMBuilder для его сборки.

Симметрично `Agent` / `AgentBuilder`: `LLM` — это `StreamSource[LLMContext, LLMEvent]`,
обёрнутый методом `stream`, а `LLMBuilder` — fluent-фасад, накапливающий
middleware, observer'ов и provider-terminal.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, Generic, Self, TypeVar

from boba.llm.events import LLMEvent
from boba.llm.middleware import AssistantAggregator
from boba.llm.models import LLMContext
from boba.llm.observer import CompositeLLMRequestObserver, LLMRequestObserver
from boba.patterns import StreamSource, StreamSourceChainBuilder

__all__ = ["LLM", "LLMBuilder"]


TRequest = TypeVar("TRequest")
TChunk = TypeVar("TChunk")
TApiError = TypeVar("TApiError", bound=Exception)
THttpError = TypeVar("THttpError", bound=Exception)

MiddlewareFactory = Callable[
    [StreamSource[LLMContext, LLMEvent]],
    StreamSource[LLMContext, LLMEvent],
]
TerminalFactory = Callable[
    [LLMRequestObserver[Any, Any, Any, Any]],
    StreamSource[LLMContext, LLMEvent],
]


class LLM:
    """Тонкий оркестратор LLM-цепочки: один `stream`, никакого loop.

    Симметрично `Agent`: оборачивает `StreamSource[LLMContext, LLMEvent]`
    и сбрасывает его на каждый вызов.
    """

    def __init__(self, source: StreamSource[LLMContext, LLMEvent]) -> None:
        self._source = source

    def name(self) -> str:
        return "LLM"

    def stream(self, ctx: LLMContext) -> Iterator[LLMEvent]:
        """Прогнать один LLM-вызов; ленивый итератор LLMEvent."""
        self._source.reset()
        yield from self._source.stream(ctx)


class LLMBuilder(Generic[TRequest, TChunk, TApiError, THttpError]):
    """
    Fluent-фасад LLM-слоя: middleware + observers + provider-terminal
    - `.use_middleware(...)` — добавление конкретного middleware в цепочку
    - `.add_observer(...)`   — добавление наблюдателя запросов/ответов llm
                                 например метрика, логирование, подсчеты
    - `.build(factory)`      — собрать LLM, terminal-фабрика обязательна
    """

    def __init__(self) -> None:
        self._observers: list[
            LLMRequestObserver[TRequest, TChunk, TApiError, THttpError]
        ] = []
        self._middlewares: list[MiddlewareFactory] = []

    def add_observer(
        self,
        observer: LLMRequestObserver[TRequest, TChunk, TApiError, THttpError],
    ) -> Self:
        """Подключить наблюдатель wire-уровня; склеиваются в Composite."""
        self._observers.append(observer)
        return self

    def use_middleware(self, factory: MiddlewareFactory) -> Self:
        """
        Зарегистрировать middleware
        """
        self._middlewares.append(factory)
        return self

    def build(self, factory: TerminalFactory) -> LLM:
        """
        Собрать LLM. `factory` — terminal-фабрика провайдера,
        получает composite-observer и возвращает provider-terminal
        (см. `use_openai` и аналоги в provider-пакетах).
        """
        chain_builder = StreamSourceChainBuilder[LLMContext, LLMEvent]()

        for mw in self._middlewares:
            chain_builder.use(mw)

        terminal = factory(
            CompositeLLMRequestObserver(self._observers),
        )

        return LLM(chain_builder.terminal(AssistantAggregator(terminal)))
