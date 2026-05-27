"""LLM — тонкий оркестратор LLM-цепочки + LLMBuilder для его сборки.

Симметрично `Agent` / `AgentBuilder`: `LLM` — это `StreamSource[LLMContext, LLMEvent]`,
обёрнутый методом `stream`, а `LLMBuilder` — fluent-фасад, накапливающий
middleware, observer'ов и provider-terminal.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, Generic, Self, TypeVar

from boba.llm.events import LLMEvent
from boba.llm.models import LLMContext
from boba.llm.observer import CompositeLLMRequestObserver, LLMRequestObserver
from boba.patterns import StreamSource, StreamSourceChainBuilder

__all__ = ["LLM", "LLMBuilder"]


TRequest = TypeVar("TRequest")
TChunk = TypeVar("TChunk")
TResponse = TypeVar("TResponse")
TApiError = TypeVar("TApiError", bound=Exception)
THttpError = TypeVar("THttpError", bound=Exception)

MiddlewareFactory = Callable[
    [StreamSource[LLMContext, LLMEvent]],
    StreamSource[LLMContext, LLMEvent],
]
TerminalFactory = Callable[
    [LLMRequestObserver[Any, Any, Any, Any, Any]],
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


class LLMBuilder(Generic[TRequest, TChunk, TResponse, TApiError, THttpError]):
    """
    Fluent-фасад LLM-слоя: middleware + observers + provider-terminal

    Цепочка собирается так (сверху вниз; верхний видит события первым):

        user middlewares           ← `.use_middleware(...)`
        terminal (OpenAI/Ollama)

    Итоговые события (`*Complete` + `LLMTotalMessage`) формирует сам
    provider-terminal (консьюмер ответа) — отдельного агрегатора в цепочке нет.

    - `.use_middleware(...)`  — middleware над terminal'ом (видит весь поток
      событий: delta + итоговые).
    - `.add_observer(...)`    — наблюдатель wire-уровня.
    - `.build(factory)`       — собрать LLM, terminal-фабрика обязательна.
    """

    def __init__(self) -> None:
        self._observers: list[
            LLMRequestObserver[TRequest, TChunk, TResponse, TApiError, THttpError]
        ] = []
        self._middlewares: list[MiddlewareFactory] = []

    def add_observer(
        self,
        observer: LLMRequestObserver[
            TRequest, TChunk, TResponse, TApiError, THttpError
        ],
    ) -> Self:
        """Подключить наблюдатель wire-уровня; склеиваются в Composite."""
        self._observers.append(observer)
        return self

    def use_middleware(self, factory: MiddlewareFactory) -> Self:
        """Зарегистрировать middleware над provider-terminal'ом."""
        self._middlewares.append(factory)
        return self

    def build(self, factory: TerminalFactory) -> LLM:
        """
        Собрать LLM. `factory` — terminal-фабрика провайдера,
        получает composite-observer и возвращает provider-terminal
        (см. `use_openai` и аналоги в provider-пакетах).
        """
        terminal = factory(
            CompositeLLMRequestObserver(self._observers),
        )

        chain_builder = StreamSourceChainBuilder[LLMContext, LLMEvent]()
        for mw in self._middlewares:
            chain_builder.use(mw)

        return LLM(chain_builder.terminal(terminal))
