from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar, Iterator

TName = TypeVar("TName")


class Id(Generic[TName]):
    """Базовый value object для идентификаторов."""

    def __init__(self, name: TName) -> None:
        self._name = name

    @property
    def name(self) -> TName:
        return self._name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, self.__class__) and self._name == other._name

    def __hash__(self) -> int:
        return hash(self._name)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self._name!r})"


TValue = TypeVar("TValue")


class Validator(ABC, Generic[TValue]):
    """
    Валидатор + нормализатор.
    Принимает значение, возвращает нормализованное.
    Бросает исключение, если значение невалидно.
    """

    @abstractmethod
    def validate(self, value: TValue) -> TValue: ...


TCandidate = TypeVar("TCandidate")


class Specification(ABC, Generic[TCandidate]):
    """
    Бизнес-правило как объект.
    Проверяет, удовлетворяет ли кандидат условию.
    """

    @abstractmethod
    def is_satisfied_by(self, candidate: TCandidate) -> bool: ...

    def and_(self, other: Specification[TCandidate]) -> Specification[TCandidate]:
        return _AndSpec(self, other)

    def or_(self, other: Specification[TCandidate]) -> Specification[TCandidate]:
        return _OrSpec(self, other)

    def not_(self) -> Specification[TCandidate]:
        return _NotSpec(self)


class _AndSpec(Specification[TCandidate]):
    def __init__(
        self, left: Specification[TCandidate], right: Specification[TCandidate]
    ) -> None:
        self._left = left
        self._right = right

    def is_satisfied_by(self, candidate: TCandidate) -> bool:
        return self._left.is_satisfied_by(candidate) and self._right.is_satisfied_by(
            candidate
        )


class _OrSpec(Specification[TCandidate]):
    def __init__(
        self, left: Specification[TCandidate], right: Specification[TCandidate]
    ) -> None:
        self._left = left
        self._right = right

    def is_satisfied_by(self, candidate: TCandidate) -> bool:
        return self._left.is_satisfied_by(candidate) or self._right.is_satisfied_by(
            candidate
        )


class _NotSpec(Specification[TCandidate]):
    def __init__(self, spec: Specification[TCandidate]) -> None:
        self._spec = spec

    def is_satisfied_by(self, candidate: TCandidate) -> bool:
        return not self._spec.is_satisfied_by(candidate)


TContext = TypeVar("TContext")
TId = TypeVar("TId", bound=Id)
TState = TypeVar("TState")


class Provider(ABC, Generic[TId, TContext, TState]):
    """
    Именованный поставщик с приоритетом.
    Дополняет состояние сборки: видит результат предыдущих провайдеров.
    """

    @abstractmethod
    def id(self) -> TId: ...

    @abstractmethod
    def priority(self) -> int: ...

    @abstractmethod
    def apply(self, ctx: TContext, state: TState) -> TState: ...


TResult = TypeVar("TResult")


class Builder(ABC, Generic[TContext, TResult]):
    """
    Классический Builder.
    Определяет процесс сборки объекта из контекста.
    """

    @abstractmethod
    def build(self, ctx: TContext) -> TResult: ...


class CompositeBuilder(
    Builder[TContext, TResult], Generic[TId, TContext, TState, TResult]
):
    """
    Реестр провайдеров + fold + финализация.
    Провайдеры применяются последовательно (по priority),
    каждый видит и дополняет состояние предыдущих.
    """

    def __init__(self) -> None:
        self._providers: dict[TId, Provider[TId, TContext, TState]] = {}

    def register(self, provider: Provider[TId, TContext, TState]) -> None:
        self._providers[provider.id()] = provider

    def unregister(self, id: TId) -> None:
        self._providers.pop(id, None)

    def providers(self) -> Iterator[Provider[TId, TContext, TState]]:
        return iter(self._providers.values())

    @abstractmethod
    def initial(self, ctx: TContext) -> TState:
        """Начальное состояние сборки."""
        ...

    @abstractmethod
    def finalize(self, state: TState) -> TResult:
        """Превратить накопленное состояние в результат."""
        ...

    def build(self, ctx: TContext) -> TResult:
        state = self.initial(ctx)
        for p in sorted(self._providers.values(), key=lambda p: p.priority()):
            state = p.apply(ctx, state)
        return self.finalize(state)


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


class StreamSink(ABC, Generic[TEvent]):
    """
    Потребитель событий.
        handle() обрабатывает одно событие.
        consume() итерирует поток, вызывая handle() для каждого события.
        reset() сбрасывает состояние. По умолчанию — ничего не делает.
    """

    @abstractmethod
    def name(self) -> str: ...

    def reset(self) -> None:
        pass

    @abstractmethod
    def handle(self, event: TEvent) -> None: ...

    def consume(self, stream: Iterator[TEvent]) -> None:
        for event in stream:
            self.handle(event)


class CompositeSink(StreamSink[TEvent]):
    """Fan-out: раздаёт каждое событие всем вложенным sink'ам."""

    def __init__(self, sinks: list[StreamSink[TEvent]]) -> None:
        self._sinks = list(sinks)

    def name(self) -> str:
        return "CompositeSink(" + ", ".join(s.name() for s in self._sinks) + ")"

    def reset(self) -> None:
        for sink in self._sinks:
            sink.reset()

    def handle(self, event: TEvent) -> None:
        for sink in self._sinks:
            sink.handle(event)


TIn = TypeVar("TIn")
TOut = TypeVar("TOut")


class Converter(ABC, Generic[TIn, TOut]):
    """
    Однонаправленная конвертация 1:1. A → B.
    """

    @abstractmethod
    def convert(self, value: TIn) -> TOut: ...


class Serializer(Generic[TIn, TOut]):
    """
    Двусторонняя конвертация = композиция двух Converter.
        serialize() — прямое направление (encoder).
        deserialize() — обратное направление (decoder).
    """

    def __init__(
        self,
        encoder: Converter[TIn, TOut],
        decoder: Converter[TOut, TIn],
    ) -> None:
        self.encoder = encoder
        self.decoder = decoder

    def serialize(self, obj: TIn) -> TOut:
        return self.encoder.convert(obj)

    def deserialize(self, raw: TOut) -> TIn:
        return self.decoder.convert(raw)


class StreamConverter(ABC, Generic[TIn, TOut]):
    """
    Потоковая конвертация N:M. Iterator[A] → Iterator[B].
    """

    @abstractmethod
    def convert(self, stream: Iterator[TIn]) -> Iterator[TOut]: ...


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


class StopCondition(ABC, Generic[TContext, TEvent]):
    """
    Условие остановки цикла.
        should_stop() проверяет, нужно ли остановить цикл.
        Композиция: or_(), and_().
    """

    @abstractmethod
    def should_stop(self, ctx: TContext, event: TEvent) -> bool: ...

    def or_(self, other: StopCondition[TContext, TEvent]) -> StopCondition[TContext, TEvent]:
        return _OrStop(self, other)

    def and_(self, other: StopCondition[TContext, TEvent]) -> StopCondition[TContext, TEvent]:
        return _AndStop(self, other)


class _OrStop(StopCondition[TContext, TEvent]):
    def __init__(self, left: StopCondition[TContext, TEvent], right: StopCondition[TContext, TEvent]) -> None:
        self._left = left
        self._right = right

    def should_stop(self, ctx: TContext, event: TEvent) -> bool:
        return self._left.should_stop(ctx, event) or self._right.should_stop(ctx, event)


class _AndStop(StopCondition[TContext, TEvent]):
    def __init__(self, left: StopCondition[TContext, TEvent], right: StopCondition[TContext, TEvent]) -> None:
        self._left = left
        self._right = right

    def should_stop(self, ctx: TContext, event: TEvent) -> bool:
        return self._left.should_stop(ctx, event) and self._right.should_stop(ctx, event)


class Loop(StreamSource[TContext, TEvent]):
    """
    Оркестратор, который запускает source в цикле, пока stop condition не сработает.
    """

    def __init__(
        self,
        source: StreamSource[TContext, TEvent],
        stop: StopCondition[TContext, TEvent],
    ) -> None:
        self._source = source
        self._stop = stop

    def name(self) -> str:
        return "Loop(" + self._source.name() + ")"

    def produce(self, ctx: TContext) -> Iterator[TEvent]:
        while True:
            for event in self._source.produce(ctx):
                yield event

                if self._stop.should_stop(ctx, event):
                    return
