"""Базовые runtime-Converter-ы общего назначения.

- Identity-проверки (OneOf/MinValue/NonEmpty) — Converter[A, A].
- Type-narrowing (ParseInt/IsString) — Converter[A, B].
- Default/Required работают с MISSING-сентинелом.

Контракт ошибок: convert бросает только ConverterInputError (семантический
отказ) или ConverterOutputError (баг реализации) — никаких ValueError/TypeError.

Валидаторы, выражающие правило в wire-схеме, реализуют SchemaContributor.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sized
from typing import Any, ClassVar, Final, Generic, TypeVar

from boba.domain.core.patterns import Converter, ConverterInputError
from boba.domain.core.schema import ParamWireSchema, SchemaContributor

__all__ = [
    "MISSING",
    "ChainConverter",
    "Default",
    "IsBool",
    "IsInt",
    "IsNumber",
    "IsString",
    "MaxLength",
    "MaxValue",
    "MinLength",
    "MinValue",
    "MutuallyExclusive",
    "NonEmpty",
    "Nullable",
    "OneOf",
    "Ordered",
    "ParseBool",
    "ParseCsvList",
    "ParseFloat",
    "ParseInt",
    "ParseString",
    "Pass",
    "Required",
    "RequiresTogether",
    "ValueConverter",
]


T = TypeVar("T")


# ═════════════════════════════════════════════════════════════════════
#  MISSING — sentinel для «значения не было»
# ═════════════════════════════════════════════════════════════════════


class _MissingType:
    """Sentinel-тип для значения «значения не было».

    Singleton; единственный инстанс — MISSING. Отличает «ключа
    не было в источнике» от «ключ был, но значение None».
    """

    _instance: ClassVar[_MissingType | None] = None

    def __new__(cls) -> _MissingType:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Final = _MissingType()


# ═════════════════════════════════════════════════════════════════════
#  Identity / composition
# ═════════════════════════════════════════════════════════════════════


class Pass(Converter[Any, Any]):
    """No-op конвертер: возвращает значение как есть, всегда успех.

    Используется как явный «без правил» вместо None в местах, где
    Converter обязателен по контракту (например,
    invariants).
    """

    def convert(self, value: Any) -> Any:
        return value


class ChainConverter(Converter[Any, T], SchemaContributor, Generic[T]):
    """Последовательно применяет конвертеры, прокидывая результат.

    Тип результата определяет финальный шаг цепочки; промежуточные шаги
    могут менять тип значения свободно. Из-за variadic-композиции
    pyright статически не доказывает корректность связки шагов — это
    осознанный компромисс (как в Pydantic-like pipeline'ах).

    Первое падение прерывает цепочку. contribute делегируется всем
    участникам, реализующим SchemaContributor. Порядок
    регистрации = порядок применения и к значению, и к схеме (последний
    может переопределить поле, заданное предыдущим).

    Пустая цепочка (ChainConverter()) — no-op-identity: пропускает
    любое значение, ничего не вкладывает в схему.
    """

    def __init__(self, *converters: Converter[Any, Any]) -> None:
        self._converters = converters

    def convert(self, value: Any) -> T:
        for c in self._converters:
            value = c.convert(value)
        return value  # type: ignore[no-any-return]

    def contribute(self, schema: ParamWireSchema) -> None:
        for c in self._converters:
            if isinstance(c, SchemaContributor):
                c.contribute(schema)


# ═════════════════════════════════════════════════════════════════════
#  Missing-aware: Required / Default / ValueConverter
# ═════════════════════════════════════════════════════════════════════


class Required(Converter[Any, Any], SchemaContributor):
    """Параметр обязателен. Бросает на MISSING и на None.

    Помечает параметр как required в wire-схеме.
    """

    def convert(self, value: Any) -> Any:
        if value is MISSING:
            raise ConverterInputError(
                "параметр обязателен — значение не передано"
            )
        if value is None:
            raise ConverterInputError("параметр обязателен — null недопустим")
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.required = True


class Default(Converter[Any, Any], SchemaContributor):
    """Подставить значение по умолчанию, если пришло MISSING.

    На None не реагирует — это явное «нет значения», не пропуск.
    Помечает default в wire-схеме.
    """

    def __init__(self, value: Any) -> None:
        self._value = value

    def convert(self, value: Any) -> Any:
        if value is MISSING:
            return self._value
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["default"] = self._value


class Nullable(Converter[Any, Any], SchemaContributor):
    """Wrapper: при MISSING/None на входе — возвращает None,
    иначе делегирует во внутренний конвертер.

    Удобно для конфиг-полей с типом T | None: чейн вида
    ChainConverter(Default(None), ParseX()) ломался бы тем, что после
    Default(None) ParseX получает None и пытается его
    привести к T (для ParseString это str(None) == "None").
    Nullable(ParseX()) разрывает цепочку на null и сразу возвращает
    None, в остальных случаях прогоняя значение через ParseX.

    Контракт wire-схемы: делегируется внутреннему конвертеру; реализации,
    которым важно отметить «nullable» отдельно, могут добавить метку
    после композиции.
    """

    def __init__(self, inner: Converter[Any, Any]) -> None:
        self._inner = inner

    def convert(self, value: Any) -> Any:
        if value is MISSING or value is None:
            return None
        return self._inner.convert(value)

    def contribute(self, schema: ParamWireSchema) -> None:
        if isinstance(self._inner, SchemaContributor):
            self._inner.contribute(schema)


class ValueConverter(Converter[Any, Any]):
    """База тип-конвертеров: пропускают MISSING без проверки.

    Полезно, когда параметр опциональный и Required/Default
    в цепочке нет — значит MISSING должен дойти до оркестратора как
    «значения нет», а не упасть на проверке типа.

    Наследник реализует _convert_value для не-MISSING случая.
    """

    def convert(self, value: Any) -> Any:
        if value is MISSING:
            return MISSING
        return self._convert_value(value)

    @abstractmethod
    def _convert_value(self, value: Any) -> Any: ...


# ═════════════════════════════════════════════════════════════════════
#  Parse* — coercion из object в типизированное (env / TOML / JSON)
# ═════════════════════════════════════════════════════════════════════


class ParseString(ValueConverter, SchemaContributor):
    """Привести любое значение к str через str(value).

    Принимает уже-str без изменений; иначе — `str(value)`. Это
    lenient-вариант для конфига (env-источник вообще даёт только str).
    Для строгой проверки «должно быть именно str» — используй
    IsString.

    В wire-схеме: type: string.
    """

    def _convert_value(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        return str(value)

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "string"


class ParseInt(ValueConverter, SchemaContributor):
    """Привести значение к int.

    Принимает int (но не bool — это семантически другой тип),
    парсит str через int(value.strip()). На остальных типах
    бросает.

    В wire-схеме: type: integer.
    """

    def _convert_value(self, value: Any) -> int:
        if isinstance(value, bool):
            raise ConverterInputError(f"expected int, got bool {value!r}")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError as exc:
                raise ConverterInputError(
                    f"not a valid int: {value!r}"
                ) from exc
        raise ConverterInputError(
            f"cannot convert {type(value).__name__} to int"
        )

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "integer"


class ParseFloat(ValueConverter, SchemaContributor):
    """Привести значение к float.

    Принимает int/float (но не bool), парсит str через
    float(value.strip()).

    В wire-схеме: type: number.
    """

    def _convert_value(self, value: Any) -> float:
        if isinstance(value, bool):
            raise ConverterInputError(f"expected float, got bool {value!r}")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError as exc:
                raise ConverterInputError(
                    f"not a valid float: {value!r}"
                ) from exc
        raise ConverterInputError(
            f"cannot convert {type(value).__name__} to float"
        )

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "number"


class ParseBool(ValueConverter, SchemaContributor):
    """Привести значение к bool.

    Принимает bool без изменений, парсит str по словарю
    (true/1/yes/on → True; false/0/no/off
    → False, регистр не важен).

    В wire-схеме: type: boolean.
    """

    _TRUE: ClassVar[frozenset[str]] = frozenset({"true", "1", "yes", "on"})
    _FALSE: ClassVar[frozenset[str]] = frozenset({"false", "0", "no", "off"})

    def _convert_value(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in self._TRUE:
                return True
            if normalized in self._FALSE:
                return False
            raise ConverterInputError(f"not a valid bool: {value!r}")
        raise ConverterInputError(
            f"cannot convert {type(value).__name__} to bool"
        )

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "boolean"


class ParseCsvList(ValueConverter, SchemaContributor):
    """Привести значение к list[str].

    list — как есть (с пустыми/None отброшенными), str — split
    по запятой с обрезкой пробелов.

    В wire-схеме: type: array (с items: {type: string}).
    """

    def _convert_value(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [
                str(item) for item in value if item is not None and str(item) != ""
            ]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        raise ConverterInputError(
            f"cannot convert {type(value).__name__} to list[str]"
        )

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "array"
        schema.property["items"] = {"type": "string"}


# ═════════════════════════════════════════════════════════════════════
#  Constraints — identity Converter[T, T] с predicate-проверкой
# ═════════════════════════════════════════════════════════════════════


class OneOf(Converter[Any, Any], SchemaContributor):
    """Значение должно быть в фиксированном наборе. В wire-схеме: enum."""

    _MIN_OPTIONS: ClassVar[int] = 1

    def __init__(self, *options: Any) -> None:
        if len(options) < self._MIN_OPTIONS:
            raise ValueError(f"OneOf требует минимум {self._MIN_OPTIONS} вариант")
        self._options = options

    def convert(self, value: Any) -> Any:
        if value not in self._options:
            raise ConverterInputError(
                f"должно быть одно из {list(self._options)}, получено {value!r}"
            )
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["enum"] = list(self._options)


class MinValue(Converter[Any, Any]):
    """Значение >= threshold. Применимо к числовым типам."""

    def __init__(self, threshold: int | float) -> None:
        self._threshold = threshold

    def convert(self, value: Any) -> Any:
        if value < self._threshold:
            raise ConverterInputError(
                f"должно быть >= {self._threshold}, получено {value}"
            )
        return value


class MaxValue(Converter[Any, Any]):
    """Значение <= threshold. Применимо к числовым типам."""

    def __init__(self, threshold: int | float) -> None:
        self._threshold = threshold

    def convert(self, value: Any) -> Any:
        if value > self._threshold:
            raise ConverterInputError(
                f"должно быть <= {self._threshold}, получено {value}"
            )
        return value


class MinLength(Converter[Any, Any]):
    """Длина >= threshold. Применимо к строкам/коллекциям."""

    def __init__(self, threshold: int) -> None:
        self._threshold = threshold

    def convert(self, value: Any) -> Any:
        if not isinstance(value, Sized):
            raise ConverterInputError(
                f"длина не определена для {type(value).__name__}"
            )
        if len(value) < self._threshold:
            raise ConverterInputError(
                f"длина должна быть >= {self._threshold}, получено {len(value)}"
            )
        return value


class MaxLength(Converter[Any, Any]):
    """Длина <= threshold. Применимо к строкам/коллекциям."""

    def __init__(self, threshold: int) -> None:
        self._threshold = threshold

    def convert(self, value: Any) -> Any:
        if not isinstance(value, Sized):
            raise ConverterInputError(
                f"длина не определена для {type(value).__name__}"
            )
        if len(value) > self._threshold:
            raise ConverterInputError(
                f"длина должна быть <= {self._threshold}, получено {len(value)}"
            )
        return value


class NonEmpty(Converter[Any, Any]):
    """Значение непустое (длина > 0). Применимо к строкам/коллекциям."""

    def convert(self, value: Any) -> Any:
        if not isinstance(value, Sized):
            raise ConverterInputError(
                f"пустота не определена для {type(value).__name__}"
            )
        if len(value) == 0:
            raise ConverterInputError("значение не должно быть пустым")

        return value


# ═════════════════════════════════════════════════════════════════════
#  Strict type-checkers — Is* (input уже типизирован, без coercion)
# ═════════════════════════════════════════════════════════════════════
#
# Различие с Parse*-семейством:
#   ParseString().convert(42)    → "42"  (lenient: str(value))
#   IsString().convert(42)       → ConverterInputError
#
# Используются там, где input уже типизирован: JSON-аргументы tool'ов
# (LLM передал их как str/int/etc), TOML-значения (типизированы парсером).
# Для config-env (всё str) больше подходит Parse*.


class IsString(ValueConverter, SchemaContributor):
    """Тип значения — строго str. В wire-схеме: type: string.

    В отличие от ParseString, не делает coercion: int или
    dict будут отвергнуты. Уместно там, где input уже типизирован —
    например, JSON-аргументы tool'ов.
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
    """Тип значения — целое число. bool отвергается отдельно
    (это семантически другой тип, хотя и подкласс int).

    В wire-схеме: type: integer.
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
    """Тип значения — число (int или float). bool отвергается.

    В wire-схеме: type: number.
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
    """Тип значения — булево. В wire-схеме: type: boolean."""

    def _convert_value(self, value: Any) -> bool:
        if not isinstance(value, bool):
            raise ConverterInputError(
                f"ожидался bool, получено {type(value).__name__}"
            )
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "boolean"


# ═════════════════════════════════════════════════════════════════════
#  Cross-field инварианты (для ObjectSchema.invariants)
# ═════════════════════════════════════════════════════════════════════
#
# Работают над dict'ом уже провалидированных полей — стандартный
# слот в ObjectSchema. Применимы и для tool-input-схем, и для
# config-секций (после унификации обе используют ObjectSchema).


class MutuallyExclusive(Converter[dict[str, Any], dict[str, Any]]):
    """Одновременно задан может быть максимум один из перечисленных полей.

    Получает dict уже провалидированных полей и проверяет, что в нём
    нет более одного ключа из names.
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
    """Перечисленные поля — либо все заданы, либо все отсутствуют."""

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
    """value[first] <= value[second], когда оба поля заданы.

    Применимо к числовым/строковым полям, сравнимым через <=.
    Если хотя бы одно из двух не задано — проверка пропускается.
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
