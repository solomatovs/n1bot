"""Валидаторы аргументов tool'ов.

Решает две задачи в одном объекте:

1. **Runtime-валидация** значения, пришедшего извне (от LLM). Используется
   паттерн :class:`Validator` из :mod:`boba.domain.core.patterns` — ``T → T``
   с выбросом :class:`ParamValidationError` при нарушении.

2. **Wire-схема** для потребителя. Валидаторы, реализующие
   :class:`SchemaContributor`, дополняют :class:`ParamWireSchema` нужными
   полями (``type``, ``enum``, ``default``, ``required``). Так нет
   дрейфа: правило валидации и описание для LLM собираются из одного
   объекта.

Композиция — :class:`ChainValidator`. No-op — :class:`Pass`.

Семантика «значение отсутствует»: оркестратор передаёт сентинел
:data:`MISSING` в первый валидатор. :class:`Required` бросит,
:class:`Default` подставит значение, value-валидаторы (:class:`ValueValidator`)
пропустят MISSING без проверки.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sized
from typing import Any, Final

from boba.domain.core.patterns import Validator
from boba.domain.core.tools.errors import (
    InvalidSchemaInvariantError,
    InvalidToolArgumentError,
)
from boba.domain.core.tools.schema import (
    ParamWireSchema,
    SchemaContributor,
    ToolId,
    ToolInputSchema,
)


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


class ParamValidationError(Exception):
    """Сырая ошибка валидатора — без контекста (имя параметра, tool_id).

    Сообщение должно быть человекочитаемым и содержать причину отказа
    («должно быть строкой», «не входит в [...]»). Контекст добавляет
    оркестратор, оборачивая в доменную ошибку.
    """


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
    """База value-валидаторов: пропускают :data:`MISSING` без проверки.

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


class OneOf(ValueValidator, SchemaContributor):
    """Значение должно быть в фиксированном наборе. В wire-схеме: ``enum``."""

    _MIN_OPTIONS = 1

    def __init__(self, *options: Any) -> None:
        if len(options) < self._MIN_OPTIONS:
            raise ValueError(f"OneOf требует минимум {self._MIN_OPTIONS} вариант")
        self._options = options

    def _validate_value(self, value: Any) -> Any:
        if value not in self._options:
            raise ParamValidationError(
                f"должно быть одно из {list(self._options)}, получено {value!r}"
            )
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["enum"] = list(self._options)


class MinValue(ValueValidator):
    """Значение >= ``threshold``. Применимо к числовым типам."""

    def __init__(self, threshold: int | float) -> None:
        self._threshold = threshold

    def _validate_value(self, value: Any) -> Any:
        if value < self._threshold:
            raise ParamValidationError(
                f"должно быть >= {self._threshold}, получено {value}"
            )
        return value


class MaxValue(ValueValidator):
    """Значение <= ``threshold``. Применимо к числовым типам."""

    def __init__(self, threshold: int | float) -> None:
        self._threshold = threshold

    def _validate_value(self, value: Any) -> Any:
        if value > self._threshold:
            raise ParamValidationError(
                f"должно быть <= {self._threshold}, получено {value}"
            )
        return value


class MinLength(ValueValidator):
    """Длина >= ``threshold``. Применимо к строкам/коллекциям."""

    def __init__(self, threshold: int) -> None:
        self._threshold = threshold

    def _validate_value(self, value: Any) -> Any:
        if not isinstance(value, Sized):
            raise ParamValidationError(
                f"длина не определена для {type(value).__name__}"
            )
        if len(value) < self._threshold:
            raise ParamValidationError(
                f"длина должна быть >= {self._threshold}, получено {len(value)}"
            )
        return value


class MaxLength(ValueValidator):
    """Длина <= ``threshold``. Применимо к строкам/коллекциям."""

    def __init__(self, threshold: int) -> None:
        self._threshold = threshold

    def _validate_value(self, value: Any) -> Any:
        if not isinstance(value, Sized):
            raise ParamValidationError(
                f"длина не определена для {type(value).__name__}"
            )
        if len(value) > self._threshold:
            raise ParamValidationError(
                f"длина должна быть <= {self._threshold}, получено {len(value)}"
            )
        return value


class NonEmpty(ValueValidator):
    """Значение непустое (длина > 0). Применимо к строкам/коллекциям."""

    def _validate_value(self, value: Any) -> Any:
        if not isinstance(value, Sized):
            raise ParamValidationError(
                f"пустота не определена для {type(value).__name__}"
            )
        if len(value) == 0:
            raise ParamValidationError("значение не должно быть пустым")
        return value


class ChainValidator(Validator[Any], SchemaContributor):
    """Последовательно применяет валидаторы, прокидывая результат.

    Первое падение прерывает цепочку. ``contribute`` делегируется всем
    участникам, реализующим :class:`SchemaContributor`. Порядок
    регистрации = порядок применения и к значению, и к схеме (последний
    может переопределить поле, заданное предыдущим).

    Пустая цепочка (``ChainValidator()``) — no-op: пропускает любое
    значение, ничего не вкладывает в схему.
    """

    def __init__(self, *validators: Validator[Any]) -> None:
        self._validators = validators

    def validate(self, value: Any) -> Any:
        for v in self._validators:
            value = v.validate(value)
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        for v in self._validators:
            if isinstance(v, SchemaContributor):
                v.contribute(schema)


class Pass(Validator[Any]):
    """No-op валидатор: возвращает значение как есть, всегда успех.

    Используется как явный «без правил» вместо None — соответствует
    принципу «строгая ParamSchema без Optional/default».
    """

    def validate(self, value: Any) -> Any:
        return value


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
