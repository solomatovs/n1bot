"""
Декларативные объекты для описания структуры и их валидации
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from boba.patterns import (
    ConverterInputError,
    MissingValueError,
)
from boba.schema.coercion.base import Coercer

__all__ = [
    "CollectionField",
    "CollectionShape",
    "FieldKind",
    "FieldPathError",
    "FieldPathMissingError",
    "FieldSpec",
    "IndexedShape",
    "InlineNestedField",
    "ItemReader",
    "KeyedShape",
    "NestedField",
    "ObjectItem",
    "ObjectSchema",
    "ScalarItem",
]


class FieldPathError(ConverterInputError):
    """ConverterInputError с привязкой к имени поля и пройденному пути."""

    def __init__(
        self,
        message: str,
        *,
        field_name: str,
        location: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.field_name = field_name
        self.location = location

    @classmethod
    def from_cause(
        cls,
        exc: ConverterInputError,
        field_name: str,
        location: tuple[str, ...] = (),
    ) -> FieldPathError:
        """Завернуть произвольный ConverterInputError в FieldPathError.

        Подкласс с маркером MissingValueError остаётся MissingValueError —
        возвращается FieldPathMissingError.
        """
        if isinstance(exc, FieldPathError):
            return exc
        message = f"field {cls._render_location(field_name, location)!r}: {exc}"
        target = (
            FieldPathMissingError
            if isinstance(exc, MissingValueError)
            else FieldPathError
        )
        return target(message, field_name=field_name, location=location)

    def with_parent_location(
        self,
        parent_field: str,
        child: str,
    ) -> FieldPathError:
        """Завернуть текущую ошибку как происходящую внутри parent_field/child."""
        new_location = (child, *self.location)
        new_message = (
            f"field "
            f"{self._render_location(parent_field, new_location)!r}: "
            f"{self._strip_field_prefix(str(self))}"
        )
        return type(self)(
            new_message,
            field_name=parent_field,
            location=new_location,
        )

    @staticmethod
    def _render_location(field_name: str, location: tuple[str, ...]) -> str:
        if not location:
            return field_name
        return field_name + "." + ".".join(location)

    @staticmethod
    def _strip_field_prefix(message: str) -> str:
        """Снять «field 'xxx': » если есть."""
        if message.startswith("field '"):
            end = message.find("': ")
            if end != -1:
                return message[end + 3 :]
        return message


class FieldPathMissingError(FieldPathError, MissingValueError):
    """FieldPathError + MissingValueError-маркер."""


T = TypeVar("T")
V = TypeVar("V")
K = TypeVar("K")
R = TypeVar("R")


class FieldKind:
    """Базовая декларация поля. Конкретные виды: `FieldSpec`, `CollectionField`."""

    name: str
    description: str


@dataclass(frozen=True)
class FieldSpec(FieldKind, Generic[T]):
    """Скалярное поле: name + coercer-цепочка.

    `coercer` — пайплайн обработки значения (defaulting, parsing, валидация
    диапазонов и т.п.). Технически — `Coercer[Any, T]`-цепочка, обычно
    собираемая через `ChainCoercer(Default(...), ParseInt(), MinValue(1))`.
    Также участвует в построении wire-схемы через `SchemaContributor`.

    Обязательность поля выражается в самой цепочке через coercer'ы:
      * `Required()` (или `NotNull()` для item-level) — если MISSING →
        `MissingValueError`. `Required()` дополнительно эмитит маркер,
        который `ToolWireSchemaBuilder` поднимает в `required[]`.
      * `Default(...)` — substitute MISSING; в JSON-Schema эмитится `default`.
    """

    name: str
    coercer: Coercer[Any, T]
    description: str = ""


@dataclass(frozen=True)
class CollectionField(FieldKind, Generic[K, V, R]):
    """Поле-коллекция: ItemReader (чтение элемента) × CollectionShape (сборка)."""

    name: str
    reader: ItemReader[V]
    shape: CollectionShape[K, V, R]
    description: str = ""


@dataclass(frozen=True)
class NestedField(FieldKind, Generic[V]):
    """Поле — одиночный вложенный объект: рекурсивная схема под sub-prefix'ом.

    В отличие от CollectionField + ObjectItem (даёт Mapping или tuple),
    NestedField даёт ровно один вложенный DTO. Используется для составных
    конфигов, где один объект логически вкладывается в другой.
    """

    name: str
    schema: ObjectSchema[V]
    description: str = ""


@dataclass(frozen=True)
class InlineNestedField(FieldKind, Generic[V]):
    """Inline-вложенное поле: дочерние ключи читаются под префиксом родителя.

    В отличие от `NestedField`, который уходит в под-префикс `parent.name.X`,
    `InlineNestedField` при материализации читает дочерние поля под текущим
    префиксом (`parent.X`), но всё равно конструирует вложенный DTO и кладёт
    его в поле `name` родительского DTO. Это развязывает структуру DTO
    (композиция доменных под-DTO) и раскладку в FlatConfig (одна плоская
    секция, питающая несколько под-DTO).

    Поддерживается `FlatConfigMaterializer`. В tool-контексте
    (`ToolWireSchemaBuilder`, `ToolArgsBuilder`) сейчас не интерпретируется
    и приведёт к `NotImplementedError` — inline нужен для конфиг-источников
    с плоской раскладкой, а не для tool-API.
    """

    name: str
    schema: ObjectSchema[V]
    description: str = ""


class ItemReader(Generic[V]):
    """Чтение одного элемента коллекции. Виды: `ScalarItem`, `ObjectItem`."""


@dataclass(frozen=True)
class ScalarItem(ItemReader[T], Generic[T]):
    """Скаляр: lookup → coercer (пайплайн defaulting/parsing/валидации)."""

    coercer: Coercer[Any, T]


@dataclass(frozen=True)
class ObjectItem(ItemReader[V], Generic[V]):
    """Вложенный объект: рекурсивный Materializer / WireSchemaBuilder по схеме."""

    schema: ObjectSchema[V]


class CollectionShape(Generic[K, V, R]):
    """Форма коллекции. Виды: `IndexedShape` (tuple), `KeyedShape` (dict)."""


@dataclass(frozen=True)
class IndexedShape(CollectionShape[int, V, tuple[V, ...]], Generic[V]):
    """tuple[V, ...] по `[i]`; результат отсортирован по индексу."""


@dataclass(frozen=True)
class KeyedShape(CollectionShape[str, V, dict[str, V]], Generic[V]):
    """dict[str, V] по mapping-ключам."""


class _PassDictCoercer(Coercer[dict[str, Any], dict[str, Any]]):
    """No-op invariants по умолчанию для ObjectSchema."""

    def apply(self, value: dict[str, Any]) -> dict[str, Any]:
        return value


@dataclass(frozen=True)
class ObjectSchema(Generic[T]):
    """Декларация структуры конфига.

    Используется для двух задач (тот же декларативный объект — два сервиса):
      1) Materializer(SCHEMA).materialize(space, prefix) — типизированный DTO[T]
         из плоского FlatConfig.
      2) WireSchemaBuilder(SCHEMA).build() — JSON-Schema для tool-definition LLM.

    ── Пример: extension с динамическими tools, вложенными params и cross-field-инвариантом ──

        @dataclass(frozen=True)
        class ParamOverlay:
            description: str = ""

        @dataclass(frozen=True)
        class ToolEntry:
            enabled: bool
            description: str
            params: Mapping[str, ParamOverlay] = field(default_factory=dict)

        @dataclass(frozen=True)
        class ChromadbConfig:
            enabled: bool
            persist_path: str
            min_top_k: int
            max_top_k: int
            tools: Mapping[str, ToolEntry] = field(default_factory=dict)

        # Вложенная схема (значение для tools.<id>.params.<name>):
        PARAM_OVERLAY_SCHEMA = ObjectSchema(
            fields=[FieldSpec("description", ChainCoercer(Default(""), ParseString()))],
            factory=ParamOverlay,
        )

        # Tool overlay: содержит динамический map[str, ParamOverlay] на params:
        TOOL_ENTRY_SCHEMA = ObjectSchema(
            fields=[
                FieldSpec("enabled", ChainCoercer(Default(False), ParseBool())),
                FieldSpec(
                    "description",
                    ChainCoercer(Default(""), ParseString()),
                    description="Перекрытие описания tool'а для LLM",
                ),
                # tools.<id>.params.<name> → dict[str, ParamOverlay]
                CollectionField(
                    name="params",
                    reader=ObjectItem(PARAM_OVERLAY_SCHEMA),
                    shape=KeyedShape(),
                ),
            ],
            factory=ToolEntry,
        )

        # Корень extension'а: скаляры + cross-field-инвариант + динамические tools:
        CHROMADB_SCHEMA = ObjectSchema(
            fields=[
                FieldSpec("enabled", ChainCoercer(Default(False), ParseBool())),
                FieldSpec("persist_path", ChainCoercer(ParseString()), required=True),
                FieldSpec(
                    "min_top_k",
                    ChainCoercer(Default(1), ParseInt(), MinValue(1)),
                ),
                FieldSpec(
                    "max_top_k",
                    ChainCoercer(Default(20), ParseInt(), MaxValue(100)),
                ),
                # ext.chromadb.tools.<id> → dict[str, ToolEntry]
                CollectionField(
                    name="tools",
                    reader=ObjectItem(TOOL_ENTRY_SCHEMA),
                    shape=KeyedShape(),
                ),
            ],
            # Cross-field: гарантируем min_top_k ≤ max_top_k:
            invariants=Ordered("min_top_k", "max_top_k"),
            factory=ChromadbConfig,
            description="Параметры подключения к ChromaDB и список включённых tools.",
        )

        cfg = Materializer(CHROMADB_SCHEMA).materialize(
            space, ConfigPath.parse("ext.chromadb"),
        )                                                # → ChromadbConfig (DTO)

        wire = ToolWireSchemaBuilder(CHROMADB_SCHEMA).build()  # → dict (JSON-Schema)
    """  # noqa: E501

    fields: Sequence[FieldKind]
    invariants: Coercer[dict[str, Any], dict[str, Any]] = field(
        default_factory=_PassDictCoercer,
    )
    factory: Callable[..., T] = dict  # type: ignore[assignment]
    description: str = ""
