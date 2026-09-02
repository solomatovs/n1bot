"""Декларативные порты потокового инструмента: Inbound, Outbound, StreamSpec.

Тело инструмента объявляет каналы данных прямо в подписи —
`feed: Annotated[Inbound[AudioChunk], Injected]` для входа и
`out: Annotated[Outbound[Transcript], Injected]` для выхода. Модель
заголовка несёт свой kind литералом (`kind: Literal["audio.pcm"]`), союз
моделей разбирается дискриминатором pydantic, и каждый кадр валидируется
один раз на границе — тело работает с типизированными Framed, а не с
сырыми байтами заголовков.

Декларация — единственный источник правды о каналах инструмента: по ней
ToolMain строит порты для вызова, а StreamSpec.of_schema отдаёт хосту
интроспекцию (какие kind'ы тул принимает и отдаёт) для манифеста и
проверки стыковки цепочек. Транспортом портам служит ToolIo
(boba.toolkit.frames) — наружу он больше не показывается.

Ошибки:
PortDeclarationError — объявление порта нарушено: тип не модель заголовка,
    kind не Literal-строка, два порта одного направления.
FrameProtocolError — заголовок пришедшего кадра не подходит объявленной
    модели порта; поднимается у читателя Inbound.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from types import UnionType
from typing import (
    Annotated,
    Any,
    ClassVar,
    Generic,
    Literal,
    TypeVar,
    Union,
    get_args,
    get_origin,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from pydantic_core import CoreSchema, core_schema

from boba.toolkit.frames import FrameProtocolError, ToolIo

__all__ = [
    "Framed",
    "Inbound",
    "Outbound",
    "PortDecl",
    "PortDeclarationError",
    "PortDirection",
    "RawHead",
    "RawInbound",
    "RawOutbound",
    "StreamPorts",
    "StreamSpec",
]

HeadT = TypeVar("HeadT", bound=BaseModel)


class PortDeclarationError(Exception):
    """Подпись инструмента объявляет порт с нарушением контракта."""


class PortDirection(StrEnum):
    """Направление порта: данные в тело либо из тела."""

    INBOUND = "in"
    OUTBOUND = "out"


@dataclass(frozen=True)
class Framed(Generic[HeadT]):
    """Один принятый кадр: заголовок уже разобран в модель порта, тело —
    сырые байты (PCM, файл, текст)."""

    head: HeadT
    body: bytes


class Inbound(Generic[HeadT]):
    """Входной порт инструмента: итератор типизированных кадров до EOF.

    Тело объявляет его в подписи (`feed: Annotated[Inbound[Chunk], Injected]`)
    и просто итерируется; каждый заголовок валидируется здесь, на границе,
    против объявленной модели — битый kind поднимает FrameProtocolError у
    читателя, а не расползается по телу. Строится в ToolMain поверх ToolIo.
    """

    def __init__(self, io: ToolIo, heads: TypeAdapter[HeadT]) -> None:
        self._io = io
        self._heads = heads

    def __iter__(self) -> Iterator[Framed[HeadT]]:
        for frame in self._io.inbound():
            yield Framed(head=self._head_of(frame.header), body=frame.body)

    def _head_of(self, header: bytes) -> HeadT:
        try:
            return self._heads.validate_json(header)
        except ValidationError as exc:
            msg = f"inbound frame does not match the declared port: {exc}"
            raise FrameProtocolError(msg) from exc

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.is_instance_schema(cls)


class Outbound(Generic[HeadT]):
    """Выходной порт инструмента: emit шлёт кадр с типизированным заголовком.

    Тело объявляет его в подписи (`out: Annotated[Outbound[Reply], Injected]`)
    и зовёт emit(head, body); заголовок сериализуется моделью, тело едет
    байтами как есть. Строится в ToolMain поверх ToolIo.
    """

    def __init__(self, io: ToolIo) -> None:
        self._io = io

    def emit(self, head: HeadT, body: bytes = b"") -> None:
        self._io.emit(head, body)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.is_instance_schema(cls)


class RawHead(BaseModel):
    """Заголовок порции сырого потока: метаданных нет, только маркер raw."""

    kind: Literal["raw"] = "raw"


class RawInbound:
    """Сырой входной порт: итератор порций bytes, без моделей и валидации.

    Для passthrough-инструментов, которым структура потока не нужна: тело
    объявляет `feed: Annotated[RawInbound, Injected]` и получает тела кадров
    как есть — заголовки отбрасываются, поэтому на raw-вход можно направить
    и выход модельного порта. Строится в ToolMain поверх ToolIo.
    """

    def __init__(self, io: ToolIo) -> None:
        self._io = io

    def __iter__(self) -> Iterator[bytes]:
        for frame in self._io.inbound():
            yield frame.body

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.is_instance_schema(cls)


class RawOutbound:
    """Сырой выходной порт: write шлёт порцию bytes без объявления структур.

    Каждая порция едет кадром с маркерным заголовком RawHead — провод и
    журнал остаются кадровыми, но тело сериализацией не занимается.
    Тело объявляет `out: Annotated[RawOutbound, Injected]`; строится в
    ToolMain поверх ToolIo.
    """

    def __init__(self, io: ToolIo) -> None:
        self._io = io

    def write(self, chunk: bytes) -> None:
        self._io.emit(RawHead(), chunk)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.is_instance_schema(cls)


class StreamPorts:
    """Разбор портов из подписи инструмента и постройка их для вызова.

    Общая точка хоста и гостя: ToolArgv по is_port исключает порты из argv
    и конфига, ToolMain через build подставляет их в kwargs тела,
    StreamSpec через kinds_of собирает интроспекцию.
    """

    KIND_FIELD: ClassVar[str] = "kind"

    @staticmethod
    def is_port(annotation: Any) -> bool:
        """Параметр — порт: значение строит гость, хост его не сериализует."""
        if annotation in (RawInbound, RawOutbound):
            return True

        return get_origin(annotation) in (Inbound, Outbound)

    @classmethod
    def is_raw(cls, annotation: Any) -> bool:
        """Порт сырого потока: структур и валидации заголовков нет."""
        return annotation in (RawInbound, RawOutbound)

    @classmethod
    def direction_of(cls, annotation: Any) -> PortDirection:
        if annotation is RawInbound:
            return PortDirection.INBOUND

        if annotation is RawOutbound:
            return PortDirection.OUTBOUND

        origin = get_origin(annotation)
        if origin is Inbound:
            return PortDirection.INBOUND

        if origin is Outbound:
            return PortDirection.OUTBOUND

        msg = f"not a port annotation: {annotation!r}"
        raise PortDeclarationError(msg)

    @classmethod
    def build(
        cls, annotation: Any, io: ToolIo
    ) -> Inbound[Any] | Outbound[Any] | RawInbound | RawOutbound:
        """Порт для вызова над транспортом ToolIo."""
        if annotation is RawInbound:
            return RawInbound(io)

        if annotation is RawOutbound:
            return RawOutbound(io)

        direction = cls.direction_of(annotation)

        if direction is PortDirection.OUTBOUND:
            return Outbound(io)

        return Inbound(io, cls.head_adapter(annotation))

    @classmethod
    def head_adapter(cls, annotation: Any) -> TypeAdapter[Any]:
        """Валидатор заголовков порта: союз моделей — по дискриминатору kind."""
        members = cls._members_of(annotation)

        if len(members) == 1:
            return TypeAdapter(members[0])

        union = Union[tuple(members)]  # noqa: UP007 — динамический союз моделей
        discriminated = Annotated[union, Field(discriminator=cls.KIND_FIELD)]

        return TypeAdapter(discriminated)

    @classmethod
    def kinds_of(cls, annotation: Any) -> tuple[str, ...]:
        """Kind'ы кадров порта в порядке объявления моделей."""
        kinds: list[str] = []
        for member in cls._members_of(annotation):
            kinds.append(cls._kind_of(member))

        return tuple(kinds)

    @classmethod
    def _members_of(cls, annotation: Any) -> tuple[type[BaseModel], ...]:
        """Модели заголовков порта: одиночная либо члены союза."""
        arguments = get_args(annotation)
        if len(arguments) != 1:
            msg = f"port must declare a head model: {annotation!r}"
            raise PortDeclarationError(msg)

        head = arguments[0]

        candidates: tuple[Any, ...] = (head,)
        if get_origin(head) in (Union, UnionType):
            candidates = get_args(head)

        members: list[type[BaseModel]] = []
        for candidate in candidates:
            if not isinstance(candidate, type):
                msg = f"port head is not a model: {candidate!r}"
                raise PortDeclarationError(msg)

            if not issubclass(candidate, BaseModel):
                msg = f"port head is not a pydantic model: {candidate!r}"
                raise PortDeclarationError(msg)

            members.append(candidate)

        return tuple(members)

    @classmethod
    def _kind_of(cls, member: type[BaseModel]) -> str:
        """Kind модели заголовка: Literal-строка поля kind."""
        field = member.model_fields.get(cls.KIND_FIELD)
        if field is None:
            msg = f"head model {member.__name__} has no {cls.KIND_FIELD!r} field"
            raise PortDeclarationError(msg)

        if get_origin(field.annotation) is not Literal:
            msg = (
                f"head model {member.__name__} must declare "
                f"{cls.KIND_FIELD}: Literal[...] for the port"
            )
            raise PortDeclarationError(msg)

        values = get_args(field.annotation)
        if len(values) != 1 or not isinstance(values[0], str):
            msg = (
                f"head model {member.__name__} must declare exactly one "
                f"string kind, got {values!r}"
            )
            raise PortDeclarationError(msg)

        return values[0]


class PortDecl(BaseModel):
    """Декларация одного порта для интроспекции: имя параметра, направление
    и kind'ы кадров; raw-порт структур не объявляет — kinds пуст."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    direction: PortDirection
    kinds: tuple[str, ...]
    raw: bool


class StreamSpec(BaseModel):
    """Потоковая декларация инструмента, выведенная из его подписи.

    По ней хост узнаёт, какие kind'ы тул принимает и отдаёт, — источник для
    манифеста инструментов и проверки стыковки цепочек A -> B. Текущий
    провод несёт по одному каналу на направление, поэтому и портов не
    больше одного на сторону.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ports: tuple[PortDecl, ...] = ()

    @model_validator(mode="after")
    def _single_per_direction(self) -> StreamSpec:
        seen: set[PortDirection] = set()
        for port in self.ports:
            if port.direction in seen:
                msg = f"duplicate {port.direction} port: {port.name!r}"
                raise ValueError(msg)

            seen.add(port.direction)

        return self

    @classmethod
    def of_schema(cls, schema: type[BaseModel]) -> StreamSpec:
        """Декларация из args_schema инструмента; без портов — пустая."""
        declared: list[PortDecl] = []

        for name, field in schema.model_fields.items():
            if not StreamPorts.is_port(field.annotation):
                continue

            raw = StreamPorts.is_raw(field.annotation)

            kinds: tuple[str, ...] = ()
            if not raw:
                kinds = StreamPorts.kinds_of(field.annotation)

            declared.append(
                PortDecl(
                    name=name,
                    direction=StreamPorts.direction_of(field.annotation),
                    kinds=kinds,
                    raw=raw,
                )
            )

        return cls(ports=tuple(declared))

    def streaming(self) -> bool:
        """Инструмент объявил хотя бы один канал данных."""
        return bool(self.ports)

    def kinds(self, direction: PortDirection) -> tuple[str, ...]:
        """Kind'ы кадров направления; пусто — канала нет."""
        for port in self.ports:
            if port.direction is direction:
                return port.kinds

        return ()
