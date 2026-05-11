"""LLMSourceBuilder — fluent-фасад для сборки LLMSource поверх провайдера."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Generic, Self, TypeVar

from boba.llm.events import LLMEvent
from boba.llm.models import LLMContext
from boba.llm.observer import CompositeLLMRequestObserver, LLMRequestObserver
from boba.patterns import FactoryMethod, StreamSource

__all__ = ["LLMSource", "LLMSourceBuilder"]


LLMSource = StreamSource[LLMContext, LLMEvent]
"""Пайплайн LLM-вызова: StreamSource[LLMContext, LLMEvent]."""


TRequest = TypeVar("TRequest")
TChunk = TypeVar("TChunk")

TerminalFactory = Callable[[LLMRequestObserver[Any, Any]], LLMSource]


class LLMSourceBuilder(FactoryMethod[LLMSource], Generic[TRequest, TChunk]):
    """Накапливает observer'ы и provider-terminal; собирает LLMSource."""

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
            msg = "LLMSourceBuilder.use_terminal: terminal уже задан"
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

    def build(self) -> LLMSource:
        """Собрать LLMSource: composite-observer → terminal-factory."""
        if self._terminal_factory is None:
            msg = (
                "LLMSourceBuilder.build: terminal не задан — "
                "вызовите .use_terminal(...) или provider-extension "
                "(например, use_openai)"
            )
            raise ValueError(msg)

        observer: LLMRequestObserver[TRequest, TChunk] = (
            self._observers[0]
            if len(self._observers) == 1
            else CompositeLLMRequestObserver(self._observers)
        )

        return self._terminal_factory(observer)
