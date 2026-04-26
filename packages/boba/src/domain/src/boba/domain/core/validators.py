"""Базовые runtime-валидаторы общего назначения.

:class:`Validator` (из :mod:`boba.domain.core.patterns`) — ``T → T`` с
выбросом :class:`ParamValidationError` при нарушении. Реализации,
живущие в этом модуле, никак не привязаны к контексту вызова — те же
объекты применяются и в схемах tool'ов (после :class:`Required` /
:class:`Default` цепочки в :mod:`boba.domain.core.tools.validators`),
и в конфигурации (после :class:`Converter` в
:class:`~boba.domain.core.config.FieldSpec`).

Валидаторы, чьё правило выражается в wire-схеме, реализуют
:class:`SchemaContributor` (см. :mod:`boba.domain.core.schema`) — единый
источник правды для runtime-проверки и описания для внешнего
потребителя.
"""

from __future__ import annotations

from collections.abc import Sized
from typing import Any

from boba.domain.core.patterns import Validator
from boba.domain.core.schema import ParamWireSchema, SchemaContributor

__all__ = [
    "ChainValidator",
    "MaxLength",
    "MaxValue",
    "MinLength",
    "MinValue",
    "NonEmpty",
    "OneOf",
    "ParamValidationError",
    "Pass",
]


class ParamValidationError(Exception):
    """Сырая ошибка валидатора — без контекста (имя параметра, поля, ...).

    Сообщение должно быть человекочитаемым и содержать причину отказа
    («должно быть строкой», «не входит в [...]»). Контекст добавляет
    оркестратор: для tool-аргументов это
    :class:`~boba.domain.core.tools.errors.InvalidToolArgumentError`,
    для конфига —
    :class:`~boba.domain.core.patterns.ConverterInputError`.
    """


class Pass(Validator[Any]):
    """No-op валидатор: возвращает значение как есть, всегда успех.

    Используется как явный «без правил» вместо ``None`` в местах, где
    :class:`Validator` обязателен по контракту (например,
    :class:`~boba.domain.core.tools.schema.ToolInputSchema.invariants`).
    """

    def validate(self, value: Any) -> Any:
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


class OneOf(Validator[Any], SchemaContributor):
    """Значение должно быть в фиксированном наборе. В wire-схеме: ``enum``."""

    _MIN_OPTIONS = 1

    def __init__(self, *options: Any) -> None:
        if len(options) < self._MIN_OPTIONS:
            raise ValueError(f"OneOf требует минимум {self._MIN_OPTIONS} вариант")
        self._options = options

    def validate(self, value: Any) -> Any:
        if value not in self._options:
            raise ParamValidationError(
                f"должно быть одно из {list(self._options)}, получено {value!r}"
            )
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["enum"] = list(self._options)


class MinValue(Validator[Any]):
    """Значение >= ``threshold``. Применимо к числовым типам."""

    def __init__(self, threshold: int | float) -> None:
        self._threshold = threshold

    def validate(self, value: Any) -> Any:
        if value < self._threshold:
            raise ParamValidationError(
                f"должно быть >= {self._threshold}, получено {value}"
            )
        return value


class MaxValue(Validator[Any]):
    """Значение <= ``threshold``. Применимо к числовым типам."""

    def __init__(self, threshold: int | float) -> None:
        self._threshold = threshold

    def validate(self, value: Any) -> Any:
        if value > self._threshold:
            raise ParamValidationError(
                f"должно быть <= {self._threshold}, получено {value}"
            )
        return value


class MinLength(Validator[Any]):
    """Длина >= ``threshold``. Применимо к строкам/коллекциям."""

    def __init__(self, threshold: int) -> None:
        self._threshold = threshold

    def validate(self, value: Any) -> Any:
        if not isinstance(value, Sized):
            raise ParamValidationError(
                f"длина не определена для {type(value).__name__}"
            )
        if len(value) < self._threshold:
            raise ParamValidationError(
                f"длина должна быть >= {self._threshold}, получено {len(value)}"
            )
        return value


class MaxLength(Validator[Any]):
    """Длина <= ``threshold``. Применимо к строкам/коллекциям."""

    def __init__(self, threshold: int) -> None:
        self._threshold = threshold

    def validate(self, value: Any) -> Any:
        if not isinstance(value, Sized):
            raise ParamValidationError(
                f"длина не определена для {type(value).__name__}"
            )
        if len(value) > self._threshold:
            raise ParamValidationError(
                f"длина должна быть <= {self._threshold}, получено {len(value)}"
            )
        return value


class NonEmpty(Validator[Any]):
    """Значение непустое (длина > 0). Применимо к строкам/коллекциям."""

    def validate(self, value: Any) -> Any:
        if not isinstance(value, Sized):
            raise ParamValidationError(
                f"пустота не определена для {type(value).__name__}"
            )
        if len(value) == 0:
            raise ParamValidationError("значение не должно быть пустым")
        return value
