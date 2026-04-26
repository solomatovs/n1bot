"""Валидаторы аргументов tool'ов: tool-специфичный слой.

Часть валидаторов общего назначения вынесена в
:mod:`boba.domain.core.validators` (``Pass``, ``ChainValidator``,
``OneOf``, ``MinValue``/``MaxValue``, ``MinLength``/``MaxLength``,
``NonEmpty``, ``ParamValidationError``) и :mod:`boba.domain.core.schema`
(``ParamWireSchema``, ``SchemaContributor``) — они применимы и к
tool-аргументам, и к полям конфига.

Здесь живёт tool-specific:

- :data:`MISSING` и MISSING-aware прослойка :class:`ValueValidator`,
  на которой построены тип-валидаторы (:class:`IsString` и т.п.);
- :class:`Required` / :class:`Default` — реакция на отсутствие значения;
- cross-field валидаторы для :class:`ToolInputSchema.invariants`
  (:class:`MutuallyExclusive`, :class:`RequiresTogether`,
  :class:`Ordered`);
- top-level orchestrator :class:`SchemaArgsValidator`.

Семантика «значение отсутствует»: оркестратор передаёт сентинел
:data:`MISSING` в первый валидатор. :class:`Required` бросит,
:class:`Default` подставит значение, value-валидаторы (:class:`ValueValidator`)
пропустят MISSING без проверки. Семантические валидаторы из
``core.validators`` MISSING-skip не делают — на практике в живых
цепочках перед ними всегда стоит :class:`Required` или :class:`Default`,
поэтому MISSING до них не доходит.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, Final

from boba.domain.core.patterns import Validator
from boba.domain.core.schema import ParamWireSchema, SchemaContributor
from boba.domain.core.tools.errors import (
    InvalidSchemaInvariantError,
    InvalidToolArgumentError,
)
from boba.domain.core.tools.schema import ToolId, ToolInputSchema
from boba.domain.core.validators import ParamValidationError


class _MissingType:
    """Sentinel-тип для значения «параметр не передан».

    Singleton; единственный инстанс — :data:`MISSING`. Отличает «ключа
    не было в dict» от «ключ был, но значение None».
    """

    _instance: _MissingType | None = None

    def __new__(cls) -> _MissingType:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Final = _MissingType()


class Required(Validator[Any], SchemaContributor):
    """Параметр обязателен. Бросает на :data:`MISSING` и на ``None``.

    Помечает параметр как required в wire-схеме.
    """

    def validate(self, value: Any) -> Any:
        if value is MISSING:
            raise ParamValidationError("параметр обязателен — значение не передано")
        if value is None:
            raise ParamValidationError("параметр обязателен — null недопустим")
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.required = True


class Default(Validator[Any], SchemaContributor):
    """Подставить значение по умолчанию, если пришло :data:`MISSING`.

    На ``None`` не реагирует — это явное «нет значения», не пропуск.
    Помечает default в wire-схеме.
    """

    def __init__(self, value: Any) -> None:
        self._value = value

    def validate(self, value: Any) -> Any:
        if value is MISSING:
            return self._value
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["default"] = self._value


class ValueValidator(Validator[Any]):
    """База тип-валидаторов: пропускают :data:`MISSING` без проверки.

    Полезно, когда параметр опциональный и ``Required``/``Default``
    в цепочке нет — значит ``MISSING`` должен дойти до оркестратора как
    «значения нет», а не упасть на проверке типа.
    """

    def validate(self, value: Any) -> Any:
        if value is MISSING:
            return MISSING
        return self._validate_value(value)

    @abstractmethod
    def _validate_value(self, value: Any) -> Any: ...


class IsString(ValueValidator, SchemaContributor):
    """Тип значения — строка. В wire-схеме: ``type: string``."""

    def _validate_value(self, value: Any) -> Any:
        if not isinstance(value, str):
            raise ParamValidationError(
                f"ожидалась строка, получено {type(value).__name__}"
            )
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "string"


class IsInt(ValueValidator, SchemaContributor):
    """Тип значения — целое число. ``bool`` отвергается отдельно.

    В wire-схеме: ``type: integer``.
    """

    def _validate_value(self, value: Any) -> Any:
        # bool — подкласс int, но семантически другой тип.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ParamValidationError(
                f"ожидалось целое число, получено {type(value).__name__}"
            )
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "integer"


class IsNumber(ValueValidator, SchemaContributor):
    """Тип значения — число (int или float). ``bool`` отвергается.

    В wire-схеме: ``type: number``.
    """

    def _validate_value(self, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParamValidationError(
                f"ожидалось число, получено {type(value).__name__}"
            )
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "number"


class IsBool(ValueValidator, SchemaContributor):
    """Тип значения — булево. В wire-схеме: ``type: boolean``."""

    def _validate_value(self, value: Any) -> Any:
        if not isinstance(value, bool):
            raise ParamValidationError(
                f"ожидался bool, получено {type(value).__name__}"
            )
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "boolean"


class MutuallyExclusive(Validator[dict[str, Any]]):
    """Одновременно задан может быть максимум один из перечисленных параметров.

    Работает на уровне ``ToolInputSchema.invariants`` — получает dict
    уже провалидированных параметров и проверяет, что в нём нет более
    одного ключа из ``names``.
    """

    _MIN_NAMES = 2

    def __init__(self, *names: str) -> None:
        if len(names) < self._MIN_NAMES:
            raise ValueError(
                f"MutuallyExclusive требует минимум {self._MIN_NAMES} имени"
            )
        self._names = names

    def validate(self, value: dict[str, Any]) -> dict[str, Any]:
        present = [n for n in self._names if n in value]
        if len(present) > 1:
            raise ParamValidationError(
                f"параметры {present} взаимоисключающие — задайте только один"
            )
        return value


class RequiresTogether(Validator[dict[str, Any]]):
    """Перечисленные параметры — либо все заданы, либо все отсутствуют."""

    _MIN_NAMES = 2

    def __init__(self, *names: str) -> None:
        if len(names) < self._MIN_NAMES:
            raise ValueError(
                f"RequiresTogether требует минимум {self._MIN_NAMES} имени"
            )
        self._names = names

    def validate(self, value: dict[str, Any]) -> dict[str, Any]:
        present = [n for n in self._names if n in value]
        if present and len(present) != len(self._names):
            missing = [n for n in self._names if n not in value]
            raise ParamValidationError(
                f"параметры {list(self._names)} должны быть заданы вместе; "
                f"отсутствуют: {missing}"
            )
        return value


class Ordered(Validator[dict[str, Any]]):
    """``value[first] <= value[second]``, когда оба параметра заданы.

    Применимо к числовым/строковым параметрам, сравнимым через ``<=``.
    Если хотя бы один из двух не задан — проверка пропускается.
    """

    def __init__(self, first: str, second: str) -> None:
        self._first = first
        self._second = second

    def validate(self, value: dict[str, Any]) -> dict[str, Any]:
        if (
            self._first in value
            and self._second in value
            and value[self._first] > value[self._second]
        ):
            raise ParamValidationError(
                f"{self._first}={value[self._first]!r} должно быть <= "
                f"{self._second}={value[self._second]!r}"
            )
        return value


class SchemaArgsValidator(Validator[dict[str, Any]]):
    """Валидирует сырой dict аргументов против :class:`ToolInputSchema`.

    Контракт:
    - проверяет, что все ключи входного dict известны схеме; иначе
      :class:`InvalidToolArgumentError`;
    - для каждого param в схеме: достаёт значение (или :data:`MISSING`),
      прогоняет через ``param.validator``; ``ParamValidationError``
      оборачивает в :class:`InvalidToolArgumentError` с именем
      параметра и tool_id;
    - значения, оставшиеся :data:`MISSING` после валидации, в результат
      не попадают (опциональный параметр без default'а);
    - в финале прогоняет ``schema.invariants`` над собранным dict'ом;
      ``ParamValidationError`` оттуда оборачивает в
      :class:`InvalidSchemaInvariantError`.
    """

    def __init__(self, schema: ToolInputSchema, tool_id: ToolId) -> None:
        self._schema = schema
        self._tool_id = tool_id
        self._known: frozenset[str] = frozenset(p.name for p in schema.params)

    def validate(self, value: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(value.keys()) - self._known)
        if unknown:
            raise InvalidToolArgumentError(
                self._tool_id,
                unknown[0],
                f"неизвестный параметр (известные: {sorted(self._known)})",
            )

        result: dict[str, Any] = {}
        for param in self._schema.params:
            raw = value.get(param.name, MISSING)
            try:
                validated = param.validator.validate(raw)
            except ParamValidationError as e:
                raise InvalidToolArgumentError(self._tool_id, param.name, str(e)) from e
            if validated is not MISSING:
                result[param.name] = validated

        try:
            return self._schema.invariants.validate(result)
        except ParamValidationError as e:
            raise InvalidSchemaInvariantError(self._tool_id, str(e)) from e
