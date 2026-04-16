from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Generic, Self, TypeVar
from uuid import UUID, uuid4

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


class UuId(Id[UUID]):
    """Базовый UUID-идентификатор."""

    @classmethod
    def new(cls) -> Self:
        return cls(uuid4())

    @classmethod
    def from_uuid(cls, _id: UUID) -> Self:
        return cls(_id)

TCtx = TypeVar("TCtx")
TIn = TypeVar("TIn")
TId = TypeVar("TId", bound=Id)
TState = TypeVar("TState")
TOut = TypeVar("TOut")
TValue = TypeVar("TValue")


class StateLess(ABC):
    """
    Признак того, что объект не имеет внутреннего состояния
    не зависит от порядка вызовов
    """

    @abstractmethod
    def name(self) -> str: ...


class StateFull(StateLess):
    """
    Признак того, что объект имеет внутреннее состояние
    зависит от порядка вызовов
    """

    def reset(self) -> None:
        pass


class Validator(ABC, Generic[TValue]):
    """
    Валидатор + нормализатор.
    Принимает значение, возвращает нормализованное.
    Бросает исключение, если значение невалидно.
    """

    @abstractmethod
    def validate(self, value: TValue) -> TValue: ...


class Converter(ABC, Generic[TIn, TOut]):
    """
    Однонаправленная конвертация 1:1. A → B.
    """

    @abstractmethod
    def convert(self, value: TIn) -> TOut: ...


class StreamConverter(ABC, Generic[TIn, TOut]):
    """
    Потоковая конвертация N:M. Iterable[A] → Iterable[B].
    """

    @abstractmethod
    def convert(self, stream: Iterable[TIn]) -> Iterable[TOut]: ...


class Stream(StateFull, Generic[TCtx, TIn, TOut]):
    """ """

    @abstractmethod
    def stream(self, ctx: TCtx, stream: TIn) -> Iterable[TOut]: ...


class Provider(ABC, Generic[TId, TCtx, TState]):
    """
    Именованный поставщик с приоритетом.
    Дополняет состояние сборки: видит результат предыдущих провайдеров.
    """

    @abstractmethod
    def id(self) -> TId: ...

    @abstractmethod
    def priority(self) -> int: ...

    @abstractmethod
    def apply(self, ctx: TCtx, state: TState) -> TState: ...


class Builder(ABC, Generic[TCtx, TOut]):
    """
    Классический Builder.
    Определяет процесс сборки объекта из контекста.
    """

    @abstractmethod
    def build(self, ctx: TCtx) -> TOut: ...


class CompositeBuilder(Builder[TCtx, TOut], Generic[TId, TCtx, TState, TOut]):
    """
    Реестр провайдеров + fold + финализация.
    Провайдеры применяются последовательно (по priority),
    каждый видит и дополняет состояние предыдущих.
    """

    def __init__(self) -> None:
        self._providers: dict[TId, Provider[TId, TCtx, TState]] = {}

    def register(self, provider: Provider[TId, TCtx, TState]) -> None:
        self._providers[provider.id()] = provider

    def unregister(self, provider_id: TId) -> None:
        self._providers.pop(provider_id, None)

    def providers(self) -> Iterable[Provider[TId, TCtx, TState]]:
        return iter(self._providers.values())

    @abstractmethod
    def initial(self, ctx: TCtx) -> TState:
        """Начальное состояние сборки."""
        ...

    @abstractmethod
    def finalize(self, state: TState) -> TOut:
        """Превратить накопленное состояние в результат."""
        ...

    def build(self, ctx: TCtx) -> TOut:
        state = self.initial(ctx)
        for p in sorted(self._providers.values(), key=lambda p: p.priority()):
            state = p.apply(ctx, state)
        return self.finalize(state)


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


class Pipeline(Stream[TCtx, TIn, TOut]):
    """
    Композиция последовательно выполняющихся StreamSource (в порядке добавления).
    Каждый источник видит контекст и генерирует свои события, которые идут в общий поток
    """

    def __init__(self, stages: list[Stream[TCtx, TIn, TOut]]) -> None:
        self._stages = list(stages)

    def append(self, stage: Stream[TCtx, TIn, TOut]):
        self._stages.append(stage)
        return self

    def stage_names(self) -> list[str]:
        return [s.name() for s in self._stages]

    def name(self) -> str:
        return "Pipeline(" + " -> ".join(self.stage_names()) + ")"

    def reset(self) -> None:
        for stage in self._stages:
            stage.reset()

    def stream(self, ctx: TCtx, stream: TIn) -> Iterable[TOut]:
        """Выполнить все стадии, yield-я события по мере их появления."""

        for stage in self._stages:
            yield from stage.stream(ctx, stream)


class Specification(ABC, Generic[TValue]):
    """
    Бизнес-правило как объект.
    Проверяет, удовлетворяет ли кандидат условию.
    """

    @abstractmethod
    def is_satisfied_by(self, candidate: TValue) -> bool: ...

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

    def is_satisfied_by(self, candidate: TValue) -> bool:
        return self._left.is_satisfied_by(candidate) and self._right.is_satisfied_by(
            candidate
        )


class _OrSpec(Specification[TValue]):
    def __init__(
        self, left: Specification[TValue], right: Specification[TValue]
    ) -> None:
        self._left = left
        self._right = right

    def is_satisfied_by(self, candidate: TValue) -> bool:
        return self._left.is_satisfied_by(candidate) or self._right.is_satisfied_by(
            candidate
        )


class _NotSpec(Specification[TValue]):
    def __init__(self, spec: Specification[TValue]) -> None:
        self._spec = spec

    def is_satisfied_by(self, candidate: TValue) -> bool:
        return not self._spec.is_satisfied_by(candidate)


class Loop(Stream[TCtx, TIn, TOut]):
    """
    Композиция StreamSource + StopCondition
    Вызывает источник, итерирует его события, пока не сработает условие остановки
    """

    def __init__(
        self,
        source: Stream[TCtx, TIn, TOut],
        stop: Specification[tuple[TCtx, TOut]],
    ) -> None:
        self._source = source
        self._stop = stop

    def name(self) -> str:
        return "Loop(" + self._source.name() + ")"

    def stream(self, ctx: TCtx, stream: TIn) -> Iterable[TOut]:
        while True:
            for event in self._source.stream(ctx, stream):
                yield event

                if self._stop.is_satisfied_by((ctx, event)):
                    return
