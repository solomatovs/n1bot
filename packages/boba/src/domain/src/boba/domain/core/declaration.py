"""Декларативные примитивы описания объекта.

Универсальные строительные блоки, общие между tool-input-схемами и
config-секциями:

- :class:`FieldSpec` — самодостаточная декларация одного поля
  (``name`` + :class:`Converter`-цепочка + описание).
- :class:`ObjectSchema` — декларация объекта: набор полей +
  cross-field инварианты + фабрика финального DTO.
- :func:`validate_object` — orchestrator: читает каждое поле через
  callable-источник, прогоняет через converter, отбрасывает
  :data:`MISSING`, применяет invariants, отдаёт в factory.

Никакой адресации в глобальном namespace тут нет — это задача
владельца коллекции (для конфига —
:class:`~boba.domain.core.config.ConfigSection` с её
:attr:`namespace`; для tool-схемы —
:class:`~boba.domain.core.tools.schema.ToolDefinition`, который
агрегирует ``ObjectSchema`` без адресации).

Wire-схема (внешнее представление декларации) живёт в
:mod:`boba.domain.core.schema` (:class:`ParamWireSchema`,
:class:`ObjectWireSchema`, :class:`SchemaContributor`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from boba.domain.core.patterns import Converter, ConverterInputError
from boba.domain.core.schema import (
    ObjectWireSchema,
    ParamWireSchema,
    SchemaContributor,
)
from boba.domain.core.validators import MISSING, Pass

__all__ = [
    "FieldSpec",
    "ObjectSchema",
    "validate_object",
]


T = TypeVar("T")


@dataclass(frozen=True)
class FieldSpec(Generic[T]):
    """Декларация одного поля.

    Самодостаточно: знает только своё локальное ``name``,
    :class:`Converter`-цепочку и человекочитаемое описание. Не несёт
    адреса в глобальном namespace — это задача владельца (для конфига
    — :class:`~boba.domain.core.config.ConfigSection`, для tool-схемы —
    :class:`~boba.domain.core.tools.schema.ToolInputSchema`).

    Один и тот же класс используется и для config-полей, и для
    tool-параметров. Различие — только в том, кто складывает поля и
    как читает значение.

    ``converter`` — цепочка трансформации сырого значения в
    типизированный ``T``. Типичный шаблон::

        ChainConverter(
            Default(20),     # подставит, если источников молчат
            ParseInt(),      # привести к int (str из env, int из TOML)
            MinValue(1),     # семантическое ограничение
        )

    Реализующие :class:`SchemaContributor` шаги цепочки наполняют
    :class:`ParamWireSchema` (тип, default, required, enum, …) — единый
    источник правды для runtime-валидации и описания внешнему потребителю.
    """

    name: str
    converter: Converter[Any, T]
    description: str = ""

    def build_wire_schema(self) -> ParamWireSchema:
        """Собрать wire-описание поля через :class:`SchemaContributor`.

        Стартует с ``description`` и даёт каждому шагу конвертера
        дозаполнить ``type`` / ``enum`` / ``default`` / ``required`` и
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
    различие — только в способе чтения сырых данных (``dict`` от LLM vs
    резолвер env/TOML), которое инкапсулируется в callable передаваемом
    в :func:`validate_object`.

    Поля:

    - ``fields`` — независимые описания каждого слота (``FieldSpec``);
    - ``invariants`` — cross-field конвертер, работающий над dict'ом
      уже провалидированных полей. Проверяет инварианты, связывающие
      несколько полей: взаимоисключения, совместность, порядок. По
      умолчанию :class:`Pass` (no-op);
    - ``factory`` — фабрика финального DTO из kwargs. Для tool-args
      обычно ``dict`` (identity), для config — конкретный dataclass;
    - ``description`` — описание самого объекта/секции (для autogen
      operator-доки конфига и tool-schema'ы для LLM).

    Wire-схему агрегата строит :meth:`build_wire_schema` — итерируется
    по полям и собирает :class:`ObjectWireSchema`.
    """

    fields: Sequence[FieldSpec[Any]]
    invariants: Converter[dict[str, Any], dict[str, Any]] = field(
        default_factory=Pass,
    )
    factory: Callable[..., T] = dict  # type: ignore[assignment]
    description: str = ""

    def build_wire_schema(self) -> ObjectWireSchema:
        """JSON-Schema-подобное описание объекта.

        Каждое поле даёт ``ParamWireSchema`` через
        :meth:`FieldSpec.build_wire_schema`; итог агрегируется в
        ``{name → property-dict}`` + список ``required`` + ``description``
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
    """Прочитать каждое поле через ``read_raw``, прогнать converter,
    отбросить :data:`MISSING`-значения, применить ``invariants``,
    отдать результат в ``factory``.

    Симметричный orchestrator для tool-args (источник —
    ``dict[str, Any]`` от LLM) и для config-секций (источник —
    :class:`~boba.domain.core.config.ChainedConfigResolver`).

    Семантика ошибок: :class:`ConverterInputError` от любого шага
    пробрасывается наружу с обогащением имени поля. Caller (tools или
    config) сам оборачивает в свою domain-ошибку с дополнительным
    контекстом.
    """
    validated: dict[str, Any] = {}
    for fld in schema.fields:
        try:
            value = fld.converter.convert(read_raw(fld.name))
        except ConverterInputError as exc:
            raise ConverterInputError(
                f"field {fld.name!r}: {exc}"
            ) from exc
        if value is not MISSING:
            validated[fld.name] = value
    final = schema.invariants.convert(validated)
    return schema.factory(**final)
