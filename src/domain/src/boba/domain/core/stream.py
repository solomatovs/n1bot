from __future__ import annotations

from abc import ABC, abstractmethod
from typing import  Iterator, Generic, TypeVar, Callable


TContext = TypeVar("TContext")
TEvent = TypeVar("TEvent")

class StreamSource(ABC, Generic[TContext, TEvent]):
    """
    Источник событий
        produce() принимает контекст, возвращает итератор событий.
    """

    @abstractmethod
    def name(self) -> str:
        """Имя стадии для логирования и событий."""
        ...

    @abstractmethod
    def produce(self, ctx: TContext) -> Iterator[TEvent]: ...


class StreamSink(ABC, Generic[TContext, TEvent]):
    """
    Потребитель событий
        consume() принимает итератор событий, возвращает None.
    """

    @abstractmethod
    def name(self) -> str:
        """Имя стадии для логирования и событий."""
        ...

    @abstractmethod
    def consume(self, stream: Iterator[TEvent]) -> None: ...


TEventIn = TypeVar("TEventIn")
TEventOut = TypeVar("TEventOut")

class EventTransformer(ABC, Generic[TEventIn, TEventOut]):
    """
    Трансфопрмация потока событий.
        transform() принимает 1 событие, возвращает 1 событие. Без внутреннего состояния.
    """

    @abstractmethod
    def transform(self, item: TEventIn) -> TEventOut: ...


class StreamMiddleware(StreamSource[TContext, TEvent]):
    """
    Звено цепочки. Оборачивает следующее звено, может трансформировать события по пути.
    Может пропустить, трансформировать, или оборвать цепочку
    """

    def __init__(self, next: StreamSource[TContext, TEvent]) -> None:
        self._next = next

    @abstractmethod
    def produce(self, ctx: TContext) -> Iterator[TEvent]:
        """Получить события от следующего звена, трансформировать/фильтровать по пути, вернуть результат."""
        ...


class Pipeline(StreamSource[TContext, TEvent]):
    """
    Оркестратор, который выполняет несколько стадий подряд.
    Каждая стадия получает тот же контекст, yield-ит события.
    """

    def __init__(self, stages: list[StreamSource[TContext, TEvent]]) -> None:
        self._stages = list(stages)

    def name(self) -> str:
        return "Pipeline(" + " -> ".join(self.stage_names()) + ")"

    def stage_names(self) -> list[str]:
        return [s.name() for s in self._stages]

    def produce(self, ctx: TContext) -> Iterator[TEvent]:
        """Выполнить все стадии, yield-я события по мере их появления."""
        for stage in self._stages:
            yield from stage.produce(ctx)


class Loop(StreamSource[TContext, TEvent]):
    """
    Оркестратор, который запускает Pipeline в цикле, пока не будет сигнала остановиться
    """
    def __init__(
        self,
        source: StreamSource[TContext, TEvent],
        stop: Callable[[TEvent], bool],
    ) -> None:
        self._source = source
        self._stop = stop

    def name(self) -> str:
        return "Loop(" + self._source.name() + ")"

    def produce(self, ctx: TContext) -> Iterator[TEvent]:
        while True:
            for event in self._source.produce(ctx):
                # возвращаем все события по мере их появления, не накапливая
                yield event

                # проверяем после каждой стадии, нужно ли остановиться
                if self._stop(event):
                    return
