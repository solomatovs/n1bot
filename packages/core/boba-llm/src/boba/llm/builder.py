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
from boba.patterns import FactoryMethod, StreamSource, StreamSourceChainBuilder

__all__ = ["LLM", "LLMBuilder"]


TRequest = TypeVar("TRequest")
TChunk = TypeVar("TChunk")

MiddlewareFactory = Callable[
    [StreamSource[LLMContext, LLMEvent]],
    StreamSource[LLMContext, LLMEvent],
]
TerminalFactory = Callable[
    [LLMRequestObserver[Any, Any]],
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


class LLMBuilder(FactoryMethod[LLM], Generic[TRequest, TChunk]):
    """
    Fluent-фасад LLM-слоя: middleware + observers + provider-terminal
    - `.use(...)`           — добавление конкретного middleware в цепочку
    - `.add_observer(...)`  — добавление наблюдателя запросов/ответов llm
                                например метрика, логирование, подсчеты
    - `.use_terminal(...)`  — терминальная стадия с указанием провайдера для обработки
    """

    def __init__(self) -> None:
        self._observers: list[LLMRequestObserver[TRequest, TChunk]] = []
        self._terminal_factory: TerminalFactory | None = None
        self._middlewares: list[MiddlewareFactory] = []

    def add_observer(
        self,
        observer: LLMRequestObserver[TRequest, TChunk],
    ) -> Self:
        """Подключить наблюдатель wire-уровня; склеиваются в Composite."""
        self._observers.append(observer)
        return self

    def use(self, factory: MiddlewareFactory) -> Self:
        """Зарегистрировать middleware-factory.

        Семантика — как `AgentBuilder`/`StreamSourceChainBuilder.use`:
        `factory(inner) -> StreamSource`. Порядок регистрации = порядок
        «снаружи внутрь»: первый `use` оказывается самым внешним.
        """
        self._middlewares.append(factory)
        return self

    def use_terminal(self, factory: TerminalFactory) -> Self:
        """Зарегистрировать provider-terminal (factory принимает observer)."""
        if self._terminal_factory is not None:
            msg = "LLMBuilder.use_terminal: terminal уже задан"
            raise ValueError(msg)

        self._terminal_factory = factory
        return self

    def pipe(
        self,
        fn: Callable[..., Self],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Self:
        """Extension-style: fn(self, *args, **kwargs) -> Self."""
        return fn(self, *args, **kwargs)

    def build(self) -> LLM:
        """Собрать LLM: observer → terminal → aggregator → middleware-chain → обёртка.

        `AssistantAggregator` подключается обязательно как ближайшая обёртка
        над provider-terminal, чтобы гарантировать наличие `LLMGenerationResult`
        в любом LLM-выводе. Пользовательские middleware (включая retry) лежат
        снаружи аггрегатора — retry между попытками сбрасывает аккумулятор
        через `reset()`-цепочку.
        """
        if self._terminal_factory is None:
            msg = (
                "LLMBuilder.build: terminal не задан — "
                "вызовите .use_terminal(...) или provider-extension "
                "(например, use_openai)"
            )
            raise ValueError(msg)

        chain_builder = StreamSourceChainBuilder[LLMContext, LLMEvent]()
        for mw in self._middlewares:
            chain_builder.use(mw)

        terminal = self._terminal_factory(
            CompositeLLMRequestObserver(self._observers),
        )
        return LLM(chain_builder.terminal(AssistantAggregator(terminal)))
