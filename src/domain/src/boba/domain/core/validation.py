"""Валидаторы значений + контракт обогащения wire-схемы.

Решает две связанные задачи в одном объекте:

1. **Runtime-валидация** значения, пришедшего извне (от LLM в случае
   tool args). Используется паттерн :class:`Validator` из
   :mod:`boba.domain.core.patterns` — ``T → T`` с выбросом
   :class:`ParamValidationError` при нарушении.

2. **Wire-схема** для потребителя (LLM). Валидаторы, реализующие
   :class:`SchemaContributor`, дополняют JSON-Schema-подобный
   :class:`ParamWireSchema` нужными полями (``type``, ``enum``,
   ``default``, ``required``). Так нет дрейфа: правило валидации и
   описание для LLM собираются из одного объекта.

Композиция — через :class:`ChainValidator`. Полное отсутствие правил —
через :class:`Pass`.

Семантика «значение отсутствует»: оркестратор передаёт сентинел
:data:`MISSING` в первый валидатор. :class:`Required` бросит,
:class:`Default` подставит значение, value-валидаторы (наследники
:class:`ValueValidator`) пропустят MISSING без проверки.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sized
from dataclasses import dataclass, field
from typing import Any, Final

from boba.domain.core.patterns import Validator


class _MissingType:
    """Sentinel-тип для значения «параметр не передан».

    Отличает «ключа не было в dict» от «ключ был, но значение None».
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


@dataclass
class ParamWireSchema:
    """Wire-описание одного параметра, собираемое из валидаторов.

    ``property`` — JSON-Schema-подобный dict (``type``, ``description``,
    ``enum``, ``default``, ...). Конвертер на сторону провайдера
    (OpenAI, Anthropic) забирает его как есть.

    ``required`` — флаг «параметр обязателен»; конвертер кладёт имя
    в top-level массив ``required``.
    """

    property: dict[str, Any] = field(default_factory=dict)
    required: bool = False


class SchemaContributor(ABC):
    """Mixin: валидатор умеет дополнять :class:`ParamWireSchema`.

    Реализуется теми валидаторами, чьё правило отражается в JSON-Schema:
    :class:`Required`, :class:`Default`, :class:`OneOf`, типовые
    :class:`IsString`/:class:`IsInt`/... Композитные валидаторы
    (:class:`ChainValidator`) делегируют contribute всем участникам
    цепочки, реализующим этот контракт.
    """

    @abstractmethod
    def contribute(self, schema: ParamWireSchema) -> None:
        """Дополнить ``schema`` данными, выводимыми из этого валидатора."""
        ...


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

    def __init__(self, *options: Any) -> None:
        if not options:
            raise ValueError("OneOf требует хотя бы одного варианта")
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


# ───────── композиция ─────────


class ChainValidator(Validator[Any], SchemaContributor):
    """Последовательно применяет валидаторы, прокидывая результат.

    Первое падение прерывает цепочку. ``contribute`` делегируется всем
    участникам, реализующим :class:`SchemaContributor` — порядок
    регистрации = порядок применения к схеме (последний может
    переопределить поле, заданное предыдущим).

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


# ───────── cross-field валидаторы (работают над dict'ом аргументов) ─────────

_MIN_CROSS_FIELD_NAMES = 2


class MutuallyExclusive(Validator[dict[str, Any]]):
    """Одновременно задан может быть максимум один из перечисленных параметров.

    Работает на уровне ``ToolInputSchema.invariants`` — получает dict уже
    провалидированных по отдельности параметров и проверяет, что в нём
    нет более одного ключа из ``names``.
    """

    def __init__(self, *names: str) -> None:
        if len(names) < _MIN_CROSS_FIELD_NAMES:
            raise ValueError(
                f"MutuallyExclusive требует минимум {_MIN_CROSS_FIELD_NAMES} имени"
            )
        self._names = names

    def validate(self, value: dict[str, Any]) -> dict[str, Any]:
        present = [n for n in self._names if n in value]
        if len(present) > 1:
            raise ParamValidationError(
                f"параметры {present} взаимоисключающие — "
                f"задайте только один"
            )
        return value


class RequiresTogether(Validator[dict[str, Any]]):
    """Перечисленные параметры должны быть либо все заданы, либо все отсутствовать."""

    def __init__(self, *names: str) -> None:
        if len(names) < _MIN_CROSS_FIELD_NAMES:
            raise ValueError(
                f"RequiresTogether требует минимум {_MIN_CROSS_FIELD_NAMES} имени"
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
