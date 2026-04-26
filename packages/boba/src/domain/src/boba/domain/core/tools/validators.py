"""Tool-специфичные :class:`Converter`-ы.

Базовые converter-примитивы общего назначения вынесены в
:mod:`boba.domain.core.validators` (``MISSING``, ``Required``,
``Default``, ``ValueConverter``, ``ChainConverter``, ``OneOf``,
``Pass``, ``MinValue``/``MaxValue``, ``MinLength``/``MaxLength``,
``NonEmpty``, ``Parse*``-семейство) и :mod:`boba.domain.core.schema`
(``ParamWireSchema``, ``SchemaContributor``) — они применимы и к
tool-аргументам, и к полям конфига.

Здесь живёт tool-specific:

- строгие тип-конвертеры :class:`IsString` / :class:`IsInt` /
  :class:`IsNumber` / :class:`IsBool` (input уже типизирован
  JSON-парсером — нужна именно проверка типа, не coercion);
- cross-field конвертеры для :attr:`ToolInputSchema.invariants`
  (:class:`MutuallyExclusive`, :class:`RequiresTogether`,
  :class:`Ordered`);
- top-level orchestrator :class:`SchemaArgsValidator`.
"""

from __future__ import annotations

from typing import Any, ClassVar

from boba.domain.core.patterns import Converter, ConverterInputError
from boba.domain.core.schema import ParamWireSchema, SchemaContributor
from boba.domain.core.tools.errors import (
    InvalidSchemaInvariantError,
    InvalidToolArgumentError,
)
from boba.domain.core.tools.schema import ToolId, ToolInputSchema
from boba.domain.core.validators import MISSING, ValueConverter


# ═════════════════════════════════════════════════════════════════════
#  Строгие тип-конвертеры (identity для уже-типизированных значений)
# ═════════════════════════════════════════════════════════════════════


class IsString(ValueConverter, SchemaContributor):
    """Тип значения — строго ``str``. В wire-схеме: ``type: string``.

    В отличие от :class:`~boba.domain.core.validators.ParseString`, не
    делает coercion: ``int`` или ``dict`` будут отвергнуты. Уместно
    там, где input уже типизирован — например, JSON-аргументы tool'ов.
    """

    def _convert_value(self, value: Any) -> str:
        if not isinstance(value, str):
            raise ConverterInputError(
                f"ожидалась строка, получено {type(value).__name__}"
            )
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "string"


class IsInt(ValueConverter, SchemaContributor):
    """Тип значения — целое число. ``bool`` отвергается отдельно
    (это семантически другой тип, хотя и подкласс ``int``).

    В wire-схеме: ``type: integer``.
    """

    def _convert_value(self, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConverterInputError(
                f"ожидалось целое число, получено {type(value).__name__}"
            )
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "integer"


class IsNumber(ValueConverter, SchemaContributor):
    """Тип значения — число (``int`` или ``float``). ``bool`` отвергается.

    В wire-схеме: ``type: number``.
    """

    def _convert_value(self, value: Any) -> int | float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConverterInputError(
                f"ожидалось число, получено {type(value).__name__}"
            )
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "number"


class IsBool(ValueConverter, SchemaContributor):
    """Тип значения — булево. В wire-схеме: ``type: boolean``."""

    def _convert_value(self, value: Any) -> bool:
        if not isinstance(value, bool):
            raise ConverterInputError(
                f"ожидался bool, получено {type(value).__name__}"
            )
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "boolean"


# ═════════════════════════════════════════════════════════════════════
#  Cross-field инварианты (для ToolInputSchema.invariants)
# ═════════════════════════════════════════════════════════════════════


class MutuallyExclusive(Converter[dict[str, Any], dict[str, Any]]):
    """Одновременно задан может быть максимум один из перечисленных параметров.

    Работает на уровне :attr:`ToolInputSchema.invariants` — получает
    dict уже провалидированных параметров и проверяет, что в нём нет
    более одного ключа из ``names``.
    """

    _MIN_NAMES: ClassVar[int] = 2

    def __init__(self, *names: str) -> None:
        if len(names) < self._MIN_NAMES:
            raise ValueError(
                f"MutuallyExclusive требует минимум {self._MIN_NAMES} имени"
            )
        self._names = names

    def convert(self, value: dict[str, Any]) -> dict[str, Any]:
        present = [n for n in self._names if n in value]
        if len(present) > 1:
            raise ConverterInputError(
                f"параметры {present} взаимоисключающие — задайте только один"
            )
        return value


class RequiresTogether(Converter[dict[str, Any], dict[str, Any]]):
    """Перечисленные параметры — либо все заданы, либо все отсутствуют."""

    _MIN_NAMES: ClassVar[int] = 2

    def __init__(self, *names: str) -> None:
        if len(names) < self._MIN_NAMES:
            raise ValueError(
                f"RequiresTogether требует минимум {self._MIN_NAMES} имени"
            )
        self._names = names

    def convert(self, value: dict[str, Any]) -> dict[str, Any]:
        present = [n for n in self._names if n in value]
        if present and len(present) != len(self._names):
            missing = [n for n in self._names if n not in value]
            raise ConverterInputError(
                f"параметры {list(self._names)} должны быть заданы вместе; "
                f"отсутствуют: {missing}"
            )
        return value


class Ordered(Converter[dict[str, Any], dict[str, Any]]):
    """``value[first] <= value[second]``, когда оба параметра заданы.

    Применимо к числовым/строковым параметрам, сравнимым через ``<=``.
    Если хотя бы один из двух не задан — проверка пропускается.
    """

    def __init__(self, first: str, second: str) -> None:
        self._first = first
        self._second = second

    def convert(self, value: dict[str, Any]) -> dict[str, Any]:
        if (
            self._first in value
            and self._second in value
            and value[self._first] > value[self._second]
        ):
            raise ConverterInputError(
                f"{self._first}={value[self._first]!r} должно быть <= "
                f"{self._second}={value[self._second]!r}"
            )
        return value


# ═════════════════════════════════════════════════════════════════════
#  Top-level orchestrator
# ═════════════════════════════════════════════════════════════════════


class SchemaArgsValidator(Converter[dict[str, Any], dict[str, Any]]):
    """Валидирует сырой dict аргументов против :class:`ToolInputSchema`.

    Контракт:
    - проверяет, что все ключи входного dict известны схеме; иначе
      :class:`InvalidToolArgumentError`;
    - для каждого param в схеме: достаёт значение (или :data:`MISSING`),
      прогоняет через ``param.converter``; :class:`ConverterInputError`
      оборачивает в :class:`InvalidToolArgumentError` с именем
      параметра и tool_id;
    - значения, оставшиеся :data:`MISSING` после конвертации, в результат
      не попадают (опциональный параметр без default'а);
    - в финале прогоняет ``schema.invariants`` над собранным dict'ом;
      :class:`ConverterInputError` оттуда оборачивает в
      :class:`InvalidSchemaInvariantError`.
    """

    def __init__(self, schema: ToolInputSchema, tool_id: ToolId) -> None:
        self._schema = schema
        self._tool_id = tool_id
        self._known: frozenset[str] = frozenset(p.name for p in schema.fields)

    def convert(self, value: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(value.keys()) - self._known)
        if unknown:
            raise InvalidToolArgumentError(
                self._tool_id,
                unknown[0],
                f"неизвестный параметр (известные: {sorted(self._known)})",
            )

        # Симметрично :func:`validate_object`, но с tool-специфичными
        # обёртками ошибок (tool_id, имя параметра / признак invariants).
        result: dict[str, Any] = {}
        for param in self._schema.fields:
            raw = value.get(param.name, MISSING)
            try:
                validated = param.converter.convert(raw)
            except ConverterInputError as e:
                raise InvalidToolArgumentError(
                    self._tool_id, param.name, str(e)
                ) from e
            if validated is not MISSING:
                result[param.name] = validated

        try:
            final = self._schema.invariants.convert(result)
        except ConverterInputError as e:
            raise InvalidSchemaInvariantError(self._tool_id, str(e)) from e
        return self._schema.factory(**final)
