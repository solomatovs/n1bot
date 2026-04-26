"""Декларативные примитивы описания объекта.

Общие блоки между tool-input-схемами и config-секциями:

- FieldSpec — декларация одного поля (name + Converter + описание).
- ObjectSchema — поля + cross-field инварианты + DTO-фабрика.
- validate_object — orchestrator: читает поле, прогоняет через converter,
  отбрасывает MISSING, применяет invariants, отдаёт в factory.

Адресация в namespace — задача владельца коллекции (ConfigSection /
ToolDefinition). Wire-схема живёт в schema.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeAlias, TypeVar

from boba.domain.core.patterns import (
    Converter,
    ConverterInputError,
    MissingValueError,
)
from boba.domain.core.schema import (
    ObjectWireSchema,
    ParamWireSchema,
    SchemaContributor,
)
from boba.domain.core.validators import MISSING, Pass

__all__ = [
    "FieldAddress",
    "FieldConverterError",
    "FieldMissingError",
    "FieldSpec",
    "ObjectSchema",
    "validate_object",
]


# declaration.py живёт ниже config.py — не должен знать про ConfigKey.
# Но обязан хранить «адрес поля во внешнем namespace» для верхнего слоя
# (ConfigSection.build кладёт сюда ConfigKey, ConfigFactory достаёт его
# через isinstance(..., FieldConverterError)). Алиас object — этот слой
# содержимое адреса не интерпретирует, только переносит.
FieldAddress: TypeAlias = object


class FieldConverterError(ConverterInputError):
    """ConverterInputError, прокинутый через слой ObjectSchema.

    Несёт FieldSpec, на котором отказала конвертер-цепочка, и
    опциональный ``key`` — адрес поля во внешнем namespace (см.
    :data:`FieldAddress`). Для tool-input ``key`` остаётся None;
    для config-секций его наполняет ConfigSection.build.
    """

    def __init__(
        self,
        message: str,
        *,
        field: FieldSpec[Any],
        key: FieldAddress | None = None,
    ) -> None:
        super().__init__(message)
        self.field = field
        self.key = key


class FieldMissingError(FieldConverterError, MissingValueError):
    """FieldConverterError + MissingValueError-маркер.

    Пустой подкласс с MRO-склейкой: позволяет ловить
    ``except MissingValueError`` (диагностика «значение не задано»)
    и ``except FieldConverterError`` (имеется поле-контекст) одинаково
    — оба isinstance возвращают True. Конструктор унаследован от
    FieldConverterError; MRO C3-линеаризация склеивает диамант на
    общем предке ConverterInputError.
    """


T = TypeVar("T")


@dataclass(frozen=True)
class FieldSpec(Generic[T]):
    """Декларация одного поля.

    Самодостаточно: знает только своё локальное name,
    Converter-цепочку и человекочитаемое описание. Не несёт
    адреса в глобальном namespace — это задача владельца (для конфига
    — ConfigSection, для tool-схемы —
    ToolInputSchema).

    Один и тот же класс используется и для config-полей, и для
    tool-параметров. Различие — только в том, кто складывает поля и
    как читает значение.

    converter — цепочка трансформации сырого значения в
    типизированный T. Типичный шаблон::

        ChainConverter(
            Default(20),     # подставит, если источников молчат
            ParseInt(),      # привести к int (str из env, int из TOML)
            MinValue(1),     # семантическое ограничение
        )

    Реализующие SchemaContributor шаги цепочки наполняют
    ParamWireSchema (тип, default, required, enum, …) — единый
    источник правды для runtime-валидации и описания внешнему потребителю.
    """

    name: str
    converter: Converter[Any, T]
    description: str = ""

    def build_wire_schema(self) -> ParamWireSchema:
        """Собрать wire-описание поля через SchemaContributor.

        Стартует с description и даёт каждому шагу конвертера
        дозаполнить type / enum / default / required и
        т.п. Если шаг не реализует contributor-протокол — пропускается;
        итоговая схема содержит только то, что реально объявлено.
        """
        schema = ParamWireSchema(property={"description": self.description})
        if isinstance(self.converter, SchemaContributor):
            self.converter.contribute(schema)
        return schema


@dataclass(frozen=True)
class ObjectSchema(Generic[T]):
    """Описание объекта с именованными полями, инвариантами и фабрикой.

    Универсальный примитив для tool-input-схем и config-секций:
    различие — только в способе чтения сырых данных (dict от LLM vs
    резолвер env/TOML), которое инкапсулируется в callable передаваемом
    в validate_object.

    Поля:

    - fields — независимые описания каждого слота (FieldSpec);
    - invariants — cross-field конвертер, работающий над dict'ом
      уже провалидированных полей. Проверяет инварианты, связывающие
      несколько полей: взаимоисключения, совместность, порядок. По
      умолчанию Pass (no-op);
    - factory — фабрика финального DTO из kwargs. Для tool-args
      обычно dict (identity), для config — конкретный dataclass;
    - description — описание самого объекта/секции (для autogen
      operator-доки конфига и tool-schema'ы для LLM).

    Wire-схему агрегата строит build_wire_schema — итерируется
    по полям и собирает ObjectWireSchema.
    """

    fields: Sequence[FieldSpec[Any]]
    invariants: Converter[dict[str, Any], dict[str, Any]] = field(
        default_factory=Pass,
    )
    factory: Callable[..., T] = dict  # type: ignore[assignment]
    description: str = ""

    def build_wire_schema(self) -> ObjectWireSchema:
        """JSON-Schema-подобное описание объекта.

        Каждое поле даёт ParamWireSchema через
        build_wire_schema; итог агрегируется в
        {name → property-dict} + список required + description
        самого объекта.
        """
        schema = ObjectWireSchema(description=self.description)
        for fld in self.fields:
            wire = fld.build_wire_schema()
            schema.properties[fld.name] = dict(wire.property)
            if wire.required:
                schema.required.append(fld.name)
        return schema


def validate_object(
    schema: ObjectSchema[T],
    read_raw: Callable[[str], object],
) -> T:
    """Прочитать каждое поле через read_raw, прогнать converter,
    отбросить MISSING-значения, применить invariants,
    отдать результат в factory.

    Симметричный orchestrator для tool-args (источник —
    dict[str, Any] от LLM) и для config-секций (источник —
    ChainedConfigResolver).

    Семантика ошибок: ConverterInputError от любого шага оборачивается
    в FieldConverterError (или FieldMissingError, если внутрь упал
    MissingValueError-маркер) с привязкой FieldSpec — это даёт верхним
    слоям возможность диагностировать без парсинга текста сообщения.
    """
    validated: dict[str, Any] = {}
    for fld in schema.fields:
        try:
            value = fld.converter.convert(read_raw(fld.name))
        except ConverterInputError as exc:
            raise _wrap_field_error(exc, fld) from exc
        if value is not MISSING:
            validated[fld.name] = value
    final = schema.invariants.convert(validated)
    return schema.factory(**final)


def _wrap_field_error(
    exc: ConverterInputError,
    fld: FieldSpec[Any],
) -> FieldConverterError:
    """Завернуть ошибку конвертера в field-aware подкласс.

    Сохраняет MissingValue-маркер: упал MissingValueError →
    FieldMissingError; иначе — обычный FieldConverterError. Адрес
    (``key``) не наследуется — он принадлежит верхнему слою (ConfigSection
    знает namespace), здесь оставляем None.
    """
    message = f"field {fld.name!r}: {exc}"
    if isinstance(exc, MissingValueError):
        return FieldMissingError(message, field=fld)
    return FieldConverterError(message, field=fld)
