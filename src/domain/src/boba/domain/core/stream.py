from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator, Generic, TypeVar

TContext = TypeVar("TContext")
TEvent = TypeVar("TEvent")


class StreamSource(ABC, Generic[TContext, TEvent]):
    """
    Источник событий.
        produce() принимает контекст, возвращает итератор событий.
        reset() сбрасывает состояние. По умолчанию — ничего не делает.
    """

    @abstractmethod
    def name(self) -> str: ...

    def reset(self) -> None:
        pass

    @abstractmethod
    def produce(self, ctx: TContext) -> Iterator[TEvent]: ...


class StreamSink(ABC, Generic[TContext, TEvent]):
    """
    Потребитель событий.
        consume() принимает итератор событий, возвращает None.
        reset() сбрасывает состояние. По умолчанию — ничего не делает.
    """

    @abstractmethod
    def name(self) -> str: ...

    def reset(self) -> None:
        pass

    @abstractmethod
    def consume(self, stream: Iterator[TEvent]) -> None: ...


TEventIn = TypeVar("TEventIn")
TEventOut = TypeVar("TEventOut")


class PassiveConverter(ABC, Generic[TEventIn, TEventOut]):
    """
    Трансформация событий 1:1.
        convert() принимает 1 событие, возвращает 1 событие.
        reset() сбрасывает состояние. По умолчанию — ничего не делает.
    """

    def reset(self) -> None:
        pass

    @abstractmethod
    def convert(self, item: TEventIn) -> TEventOut: ...


class ActiveConverter(ABC, Generic[TEventIn, TEventOut]):
    """
    Трансформация потока событий N:M.
        convert() принимает 0..N событий, возвращает 0..M событий.
        reset() сбрасывает состояние. По умолчанию — ничего не делает.
    """

    def reset(self) -> None:
        pass

    @abstractmethod
    def convert(self, items: Iterator[TEventIn]) -> Iterator[TEventOut]: ...


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
    Оркестратор, который запускает source в цикле, пока should_stop не вернёт True.
    Для кастомной логики остановки — наследоваться и переопределить should_stop.
    """

    def __init__(self, source: StreamSource[TContext, TEvent]) -> None:
        self._source = source

    def name(self) -> str:
        return "Loop(" + self._source.name() + ")"

    def should_stop(self, ctx: TContext, event: TEvent) -> bool:
        """Переопределить в наследнике. По умолчанию — никогда не останавливается."""
        return False

    def produce(self, ctx: TContext) -> Iterator[TEvent]:
        while True:
            for event in self._source.produce(ctx):
                yield event

                if self.should_stop(ctx, event):
                    return
