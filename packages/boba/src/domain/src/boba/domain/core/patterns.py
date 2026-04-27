from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Sequence
from types import TracebackType
from typing import Generic, Self, TypeVar
from uuid import UUID, uuid4

TName = TypeVar("TName")


class Id(ABC, Generic[TName]):
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

    @abstractmethod
    def to_wire(self) -> str:
        """Сериализовать Id в строковое представление (JSON-safe)."""
        ...

    @classmethod
    @abstractmethod
    def from_wire(cls, value: str) -> Self:
        """Восстановить Id из строкового представления."""
        ...


class UuId(Id[UUID]):
    """Базовый UUID-идентификатор."""

    @classmethod
    def new(cls) -> Self:
        return cls(uuid4())

    @classmethod
    def from_uuid(cls, _id: UUID) -> Self:
        return cls(_id)

    def to_wire(self) -> str:
        return str(self._name)

    @classmethod
    def from_wire(cls, value: str) -> Self:
        return cls(UUID(value))


class StrId(Id[str]):
    """Строковый Id — для читабельных идентификаторов (имя секции, стадии)."""

    def to_wire(self) -> str:
        return self._name

    @classmethod
    def from_wire(cls, value: str) -> Self:
        return cls(value)


TCtx = TypeVar("TCtx")
TIn = TypeVar("TIn")
TId = TypeVar("TId", bound=Id)
TState = TypeVar("TState")
TOut = TypeVar("TOut")
# Ковариантный TOut: только output-позиции (return).
TOut_co = TypeVar("TOut_co", covariant=True)
TValue = TypeVar("TValue")
TQuery = TypeVar("TQuery")


class StateLess(ABC):
    """Объект без внутреннего состояния."""

    @abstractmethod
    def name(self) -> str: ...


class StateFull(StateLess):
    """Объект с внутренним состоянием."""

    def reset(self) -> None:
        pass


class ConverterError(Exception):
    """Базовая ошибка Converter.convert."""


class ConverterInputError(ConverterError):
    """Вход не соответствует ожидаемому формату или семантике."""


class MissingValueError(ConverterInputError):
    """Значение не было предоставлено (MISSING / null)."""


class ConverterOutputError(ConverterError):
    """Не удалось построить выход из корректного входа."""


class Converter(ABC, Generic[TIn, TOut_co]):
    """Однонаправленная конвертация 1:1. A → B."""

    @abstractmethod
    def convert(self, value: TIn) -> TOut_co:
        """Выполнить конвертацию; бросает ConverterError."""
        ...


class ContextConverter(ABC, Generic[TCtx, TIn, TOut]):
    @abstractmethod
    def convert(self, ctx: TCtx, value: TIn) -> TOut: ...


class StreamConverter(ABC, Generic[TIn, TOut]):
    """Потоковая конвертация N:M. Iterable[A] → Iterable[B]."""

    @abstractmethod
    def convert(self, stream: Iterable[TIn]) -> Iterable[TOut]: ...


class StreamSource(StateFull, Generic[TCtx, TOut]):
    """Источник потока событий: ctx → Iterable[TOut]."""

    @abstractmethod
    def stream(self, ctx: TCtx) -> Iterable[TOut]: ...


class StreamSink(StateFull, Generic[TCtx, TIn]):
    """Терминальный потребитель событий с побочными эффектами."""

    @abstractmethod
    def handle(self, ctx: TCtx, event: TIn) -> None: ...


class StreamTransformer(StateFull, Generic[TCtx, TIn, TOut]):
    """Потоковое преобразование с контекстом: Iterable[TIn] → Iterable[TOut]."""

    @abstractmethod
    def stream(self, ctx: TCtx, stream: Iterable[TIn]) -> Iterable[TOut]: ...


class Definition(ABC, Generic[TValue]):
    @abstractmethod
    def definition(self) -> TValue: ...


class Resolver(ABC, Generic[TIn, TOut]):
    @abstractmethod
    def resolve(self, req: TIn) -> TOut: ...


class Matcher(ABC, Generic[TQuery, TValue]):
    """Чистая функция (query, pool) → candidates."""

    @abstractmethod
    def match(self, query: TQuery, pool: Iterable[TValue]) -> Iterable[TValue]: ...


class Executor(ABC, Generic[TCtx, TIn, TOut]):
    """Команда-обработчик с побочными эффектами; ошибки в типе TOut."""

    @abstractmethod
    def execute(self, ctx: TCtx, req: TIn) -> TOut: ...


class FactoryMethod(ABC, Generic[TOut]):
    """Классический Factory: одношаговая сборка объекта."""

    @abstractmethod
    def build(self) -> TOut: ...


class ContextFactoryMethod(ABC, Generic[TCtx, TOut]):
    """Factory, которому для сборки нужен контекст вызова."""

    @abstractmethod
    def build(self, ctx: TCtx) -> TOut: ...


class PrioritySource(ABC, Generic[TId, TState]):
    """Одна стадия сборки для FoldFactory: state_n → state_{n+1}."""

    @abstractmethod
    def id(self) -> TId: ...

    @abstractmethod
    def priority(self) -> int: ...

    @abstractmethod
    def apply(self, state: TState) -> TState: ...


class FoldFactory(
    FactoryMethod[TOut],
    Generic[TId, TState, TOut],
):
    """FactoryMethod, собирающий объект через последовательность стадий (fold)."""

    def __init__(self) -> None:
        self._reducers: dict[TId, PrioritySource[TId, TState]] = {}

    def register(self, reducer: PrioritySource[TId, TState]) -> None:
        self._reducers[reducer.id()] = reducer

    def unregister(self, key: TId) -> None:
        self._reducers.pop(key, None)

    def providers(self) -> Iterable[PrioritySource[TId, TState]]:
        return iter(self._reducers.values())

    @abstractmethod
    def initial(self) -> TState:
        """Начальное состояние сборки."""
        ...

    @abstractmethod
    def finalize(self, state: TState) -> TOut:
        """Превратить накопленное состояние в результат."""
        ...

    def build(self) -> TOut:
        state = self.initial()

        for p in sorted(self._reducers.values(), key=lambda p: p.priority()):
            state = p.apply(state)

        return self.finalize(state)


class ContextPrioritySource(ABC, Generic[TCtx, TId, TState]):
    """Context-aware reducer для ContextFoldFactory."""

    @abstractmethod
    def id(self) -> TId: ...

    @abstractmethod
    def priority(self) -> int: ...

    @abstractmethod
    def apply(self, ctx: TCtx, state: TState) -> TState: ...


class ContextFoldFactory(
    ContextFactoryMethod[TCtx, TOut],
    Generic[TCtx, TId, TState, TOut],
):
    """ContextFactoryMethod, собирающий объект через fold-стадии с ctx."""

    def __init__(self) -> None:
        self._reducers: dict[TId, ContextPrioritySource[TCtx, TId, TState]] = {}

    def register(self, reducer: ContextPrioritySource[TCtx, TId, TState]) -> None:
        self._reducers[reducer.id()] = reducer

    def unregister(self, key: TId) -> None:
        self._reducers.pop(key, None)

    def providers(self) -> Iterable[ContextPrioritySource[TCtx, TId, TState]]:
        return iter(self._reducers.values())

    @abstractmethod
    def initial(self, ctx: TCtx) -> TState:
        """Начальное состояние сборки."""
        ...

    @abstractmethod
    def finalize(self, ctx: TCtx, state: TState) -> TOut:
        """Превратить накопленное состояние в результат."""
        ...

    def build(self, ctx: TCtx) -> TOut:
        state = self.initial(ctx)

        for p in sorted(self._reducers.values(), key=lambda p: p.priority()):
            state = p.apply(ctx, state)

        return self.finalize(ctx, state)


class Serializer(Generic[TIn, TOut]):
    """Двусторонняя конвертация = композиция двух Converter."""

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


class Specification(ABC, Generic[TValue]):
    """Бизнес-правило как объект; комбинируется через and_/or_/not_."""

    @abstractmethod
    def check(self, candidate: TValue) -> bool: ...

    def and_(self, other: Specification[TValue]) -> Specification[TValue]:
        return _AndSpec(self, other)

    def or_(self, other: Specification[TValue]) -> Specification[TValue]:
        return _OrSpec(self, other)

    def not_(self) -> Specification[TValue]:
        return _NotSpec(self)


class _AndSpec(Specification[TValue]):
    def __init__(
        self, left: Specification[TValue], right: Specification[TValue]
    ) -> None:
        self._left = left
        self._right = right

    def check(self, candidate: TValue) -> bool:
        return self._left.check(candidate) and self._right.check(candidate)


class _OrSpec(Specification[TValue]):
    def __init__(
        self, left: Specification[TValue], right: Specification[TValue]
    ) -> None:
        self._left = left
        self._right = right

    def check(self, candidate: TValue) -> bool:
        return self._left.check(candidate) or self._right.check(candidate)


class _NotSpec(Specification[TValue]):
    def __init__(self, spec: Specification[TValue]) -> None:
        self._spec = spec

    def check(self, candidate: TValue) -> bool:
        return not self._spec.check(candidate)


class ExceptionSpecification(Specification[Exception]):
    """Specification[Exception] + context manager (suppress по предикату)."""

    def and_(self, other: Specification[Exception]) -> ExceptionSpecification:
        return _ExcAndSpec(self, other)

    def or_(self, other: Specification[Exception]) -> ExceptionSpecification:
        return _ExcOrSpec(self, other)

    def not_(self) -> ExceptionSpecification:
        return _ExcNotSpec(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if exc_value is None:
            return False
        if not isinstance(exc_value, Exception):
            return False
        return self.check(exc_value)


class _ExcAndSpec(ExceptionSpecification):
    def __init__(
        self,
        left: Specification[Exception],
        right: Specification[Exception],
    ) -> None:
        self._left = left
        self._right = right

    def check(self, candidate: Exception) -> bool:
        return self._left.check(candidate) and self._right.check(candidate)


class _ExcOrSpec(ExceptionSpecification):
    def __init__(
        self,
        left: Specification[Exception],
        right: Specification[Exception],
    ) -> None:
        self._left = left
        self._right = right

    def check(self, candidate: Exception) -> bool:
        return self._left.check(candidate) or self._right.check(candidate)


class _ExcNotSpec(ExceptionSpecification):
    def __init__(self, spec: Specification[Exception]) -> None:
        self._spec = spec

    def check(self, candidate: Exception) -> bool:
        return not self._spec.check(candidate)


class IsInstance(ExceptionSpecification):
    """Проверка isinstance(exc, types) как ExceptionSpecification."""

    def __init__(self, *types: type[Exception]) -> None:
        self._types = types

    def check(self, candidate: Exception) -> bool:
        return isinstance(candidate, self._types)


class StreamSourcePipeline(StreamSource[TCtx, TOut]):
    """Sequential fan-out композиция StreamSource-ов в единый поток."""

    def __init__(
        self,
        stages: Sequence[StreamSource[TCtx, TOut]],
    ) -> None:
        self._stages = list(stages)

    def append(self, stage: StreamSource[TCtx, TOut]):
        self._stages.append(stage)
        return self

    def stage_names(self) -> Iterable[str]:
        for s in self._stages:
            yield s.name()

    def name(self) -> str:
        return "SourcePipeline({})".format(" -> ".join(self.stage_names()))

    def reset(self) -> None:
        for stage in self._stages:
            stage.reset()

    def stream(self, ctx: TCtx) -> Iterable[TOut]:
        for stage in self._stages:
            yield from stage.stream(ctx)


class StreamSinkPipeline(StreamSink[TCtx, TIn]):
    """Broadcast-композиция StreamSink-ов над одним событием."""

    def __init__(
        self,
        stages: Sequence[StreamSink[TCtx, TIn]],
    ) -> None:
        self._stages = list(stages)

    def append(self, stage: StreamSink[TCtx, TIn]):
        self._stages.append(stage)
        return self

    def stage_names(self) -> Iterable[str]:
        for s in self._stages:
            yield s.name()

    def name(self) -> str:
        return "SinkPipeline({})".format(" -> ".join(self.stage_names()))

    def reset(self) -> None:
        for stage in self._stages:
            stage.reset()

    def handle(self, ctx: TCtx, event: TIn) -> None:
        for stage in self._stages:
            stage.handle(ctx, event)


class StreamTransformerPipeline(StreamTransformer[TCtx, TIn, TOut]):
    """Sequential fan-out StreamTransformer-ов над одним входным потоком; stream должен быть re-iterable."""

    def __init__(
        self,
        stages: Sequence[StreamTransformer[TCtx, TIn, TOut]],
    ) -> None:
        self._stages: list[StreamTransformer[TCtx, TIn, TOut]] = list(stages)

    def append(self, stage: StreamTransformer[TCtx, TIn, TOut]):
        self._stages.append(stage)
        return self

    def stage_names(self) -> Iterable[str]:
        for s in self._stages:
            yield s.name()

    def name(self) -> str:
        return "TransformerPipeline({})".format(" -> ".join(self.stage_names()))

    def reset(self) -> None:
        for stage in self._stages:
            stage.reset()

    def stream(self, ctx: TCtx, stream: Iterable[TIn]) -> Iterable[TOut]:
        for stage in self._stages:
            yield from stage.stream(ctx, stream)


class StreamTransformerChain(StreamTransformer[TCtx, TIn, TIn]):
    """Chain-композиция StreamTransformer-ов одного типа (TIn → TIn)."""

    def __init__(
        self,
        stages: Sequence[StreamTransformer[TCtx, TIn, TIn]],
    ) -> None:
        self._stages: list[StreamTransformer[TCtx, TIn, TIn]] = list(stages)

    def append(
        self, stage: StreamTransformer[TCtx, TIn, TIn]
    ) -> StreamTransformerChain[TCtx, TIn]:
        self._stages.append(stage)
        return self

    def stage_names(self) -> Iterable[str]:
        for s in self._stages:
            yield s.name()

    def name(self) -> str:
        return "TransformerChain({})".format(" -> ".join(self.stage_names()))

    def reset(self) -> None:
        for stage in self._stages:
            stage.reset()

    def stream(self, ctx: TCtx, stream: Iterable[TIn]) -> Iterable[TIn]:
        for stage in self._stages:
            stream = stage.stream(ctx, stream)
        yield from stream


class StreamSourceLoop(StreamSource[TCtx, TOut]):
    """Циклический запуск StreamSource до срабатывания stop_if (проверка после yield)."""

    def __init__(
        self,
        source: StreamSource[TCtx, TOut],
        stop_if: Specification[tuple[TCtx, TOut]],
    ) -> None:
        self._source = source
        self._stop_if = stop_if

    def name(self) -> str:
        return "Loop(" + self._source.name() + ")"

    def stream(self, ctx: TCtx) -> Iterable[TOut]:
        while True:
            for event in self._source.stream(ctx):
                yield event

                if self._stop_if.check((ctx, event)):
                    return


class StreamSourceChainBuilder(Generic[TCtx, TOut]):
    """Билдер onion-цепочки StreamSource-middleware поверх обязательного терминала."""

    def __init__(self) -> None:
        self._factories: list[
            Callable[[StreamSource[TCtx, TOut]], StreamSource[TCtx, TOut]]
        ] = []

    def use(
        self,
        factory: Callable[[StreamSource[TCtx, TOut]], StreamSource[TCtx, TOut]],
    ) -> Self:
        """Зарегистрировать middleware-factory."""
        self._factories.append(factory)
        return self

    def terminal(self, terminal: StreamSource[TCtx, TOut]) -> StreamSource[TCtx, TOut]:
        """Собрать цепочку, обернув terminal зарегистрированными middleware."""
        chain = terminal
        for factory in reversed(self._factories):
            chain = factory(chain)
        return chain


class FirstMatchDispatcher(Generic[TIn, TOut]):
    """Callable-диспетчер «первое совпадение + fallback»."""

    def __init__(
        self,
        routes: Sequence[tuple[Specification[TIn], Callable[[TIn], TOut]]],
        fallback_route: Callable[[TIn], TOut],
    ) -> None:
        self._routes = list(routes)
        self._fallback_route = fallback_route

    def __call__(self, value: TIn) -> TOut:
        for spec, route in self._routes:
            if spec.check(value):
                return route(value)
        return self._fallback_route(value)


class AllMatchesDispatcher(Generic[TIn, TOut]):
    """Callable-диспетчер «все совпавшие правила → поток результатов» (lazy)."""

    def __init__(
        self,
        routes: Sequence[tuple[Specification[TIn], Callable[[TIn], TOut]]],
    ) -> None:
        self._routes = list(routes)

    def __call__(self, value: TIn) -> Iterator[TOut]:
        for spec, route in self._routes:
            if spec.check(value):
                yield route(value)


class FoldingDispatcher(Generic[TValue]):
    """Callable-диспетчер «условная цепочка трансформаций» (fold), мономорфный TValue → TValue."""

    def __init__(
        self,
        routes: Sequence[tuple[Specification[TValue], Callable[[TValue], TValue]]],
    ) -> None:
        self._routes = list(routes)

    def __call__(self, value: TValue) -> TValue:
        for spec, transform in self._routes:
            if spec.check(value):
                value = transform(value)
        return value


class FirstMatchConverter(Converter[TIn, TOut], Generic[TIn, TOut]):
    """Converter-адаптер над FirstMatchDispatcher."""

    def __init__(
        self,
        routes: Sequence[tuple[Specification[TIn], Callable[[TIn], TOut]]],
        fallback_route: Callable[[TIn], TOut],
    ) -> None:
        self._dispatch: Callable[[TIn], TOut] = FirstMatchDispatcher(
            routes, fallback_route
        )

    def convert(self, value: TIn) -> TOut:
        return self._dispatch(value)
