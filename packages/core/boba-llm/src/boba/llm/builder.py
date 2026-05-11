"""LLMPipeline: класс-обёртка над provider stream + Factory для его сборки."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, Generic, Self, TypeVar

from boba.llm.events import LLMEvent
from boba.llm.models import LLMContext
from boba.llm.observer import CompositeLLMRequestObserver, LLMRequestObserver
from boba.patterns import FactoryMethod, StreamSource

__all__ = ["LLMPipeline", "LLMPipelineFactory"]


TRequest = TypeVar("TRequest")
TChunk = TypeVar("TChunk")

TerminalFactory = Callable[
    [LLMRequestObserver[Any, Any]],
    StreamSource[LLMContext, LLMEvent],
]


class LLMPipeline:
    """Pipeline LLM-вызова: оборачивает StreamSource и стримит LLMEvent.

    Симметрично `Agent.stream(input)` — публичный API скрывает
    `StreamSource[LLMContext, LLMEvent]`-нутрянку.
    """

    def __init__(self, source: StreamSource[LLMContext, LLMEvent]) -> None:
        self._source = source

    def name(self) -> str:
        return "LLMPipeline"

    def stream(self, ctx: LLMContext) -> Iterator[LLMEvent]:
        """Прогнать один LLM-вызов; ленивый итератор LLMEvent."""
        self._source.reset()
        yield from self._source.stream(ctx)


class LLMPipelineFactory(FactoryMethod[LLMPipeline], Generic[TRequest, TChunk]):
    """Накапливает observer'ы и provider-terminal; собирает LLMPipeline."""

    def __init__(self) -> None:
        self._observers: list[LLMRequestObserver[TRequest, TChunk]] = []
        self._terminal_factory: TerminalFactory | None = None

    def add_observer(
        self,
        observer: LLMRequestObserver[TRequest, TChunk],
    ) -> Self:
        """Подключить наблюдатель wire-уровня; склеиваются в Composite."""
        self._observers.append(observer)
        return self

    def use_terminal(self, factory: TerminalFactory) -> Self:
        """Зарегистрировать provider-terminal (factory принимает observer)."""
        if self._terminal_factory is not None:
            msg = "LLMPipelineFactory.use_terminal: terminal уже задан"
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

    def build(self) -> LLMPipeline:
        """Собрать LLMPipeline: composite-observer → terminal → обёртка."""
        if self._terminal_factory is None:
            msg = (
                "LLMPipelineFactory.build: terminal не задан — "
                "вызовите .use_terminal(...) или provider-extension "
                "(например, use_openai)"
            )
            raise ValueError(msg)

        observer: LLMRequestObserver[TRequest, TChunk] = (
            self._observers[0]
            if len(self._observers) == 1
            else CompositeLLMRequestObserver(self._observers)
        )

        return LLMPipeline(self._terminal_factory(observer))
