"""Базовые runtime-Converter-ы общего назначения."""

from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import Sized
from pathlib import Path
from typing import Any, ClassVar, Final, Generic, TypeVar

from boba.domain.core.patterns import Converter, ConverterInputError, MissingValueError
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
    "ParseList",
    "ParseString",
    "Pass",
    "ReadJsonFile",
    "ReadTextFile",
    "Required",
    "RequiresTogether",
    "ValueConverter",
]


T = TypeVar("T")

class _MissingType:
    """Singleton-sentinel «значения не было» (отдельно от None)."""

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


class Pass(Converter[Any, Any]):
    """No-op конвертер: возвращает значение как есть."""

    def convert(self, value: Any) -> Any:
        return value


class ChainConverter(Converter[Any, T], SchemaContributor, Generic[T]):
    """Последовательно применяет конвертеры; contribute делегируется всем."""

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


class Required(Converter[Any, Any], SchemaContributor):
    """Параметр обязателен; MissingValueError на MISSING/None."""

    def convert(self, value: Any) -> Any:
        if value is MISSING:
            raise MissingValueError("параметр обязателен — значение не передано")
        if value is None:
            raise MissingValueError("параметр обязателен — null недопустим")
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.required = True


class Default(Converter[Any, Any], SchemaContributor):
    """Подставить значение по умолчанию, если пришло MISSING (на None не реагирует)."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def convert(self, value: Any) -> Any:
        if value is MISSING:
            return self._value
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["default"] = self._value


class Nullable(Converter[Any, Any], SchemaContributor):
    """Wrapper: MISSING/None → None, иначе делегирует во внутренний конвертер."""

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
    """База тип-конвертеров; MISSING пропускается без проверки."""

    def convert(self, value: Any) -> Any:
        if value is MISSING:
            return MISSING
        return self._convert_value(value)

    @abstractmethod
    def _convert_value(self, value: Any) -> Any: ...

class ParseString(ValueConverter, SchemaContributor):
    """Привести любое значение к str через str(value); wire: type=string."""

    def _convert_value(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        return str(value)

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "string"


class ParseInt(ValueConverter, SchemaContributor):
    """Привести значение к int (bool отвергается); wire: type=integer."""

    def _convert_value(self, value: Any) -> int:
        if isinstance(value, bool):
            raise ConverterInputError(f"expected int, got bool {value!r}")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError as exc:
                raise ConverterInputError(f"not a valid int: {value!r}") from exc
        raise ConverterInputError(f"cannot convert {type(value).__name__} to int")

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "integer"


class ParseFloat(ValueConverter, SchemaContributor):
    """Привести значение к float (bool отвергается); wire: type=number."""

    def _convert_value(self, value: Any) -> float:
        if isinstance(value, bool):
            raise ConverterInputError(f"expected float, got bool {value!r}")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError as exc:
                raise ConverterInputError(f"not a valid float: {value!r}") from exc
        raise ConverterInputError(f"cannot convert {type(value).__name__} to float")

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "number"


class ParseBool(ValueConverter, SchemaContributor):
    """Привести значение к bool (true/1/yes/on, false/0/no/off); wire: type=boolean."""

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
        raise ConverterInputError(f"cannot convert {type(value).__name__} to bool")

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "boolean"


class ParseCsvList(ValueConverter, SchemaContributor):
    """Привести значение к list[str] (split по запятой); wire: type=array."""

    def _convert_value(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if item is not None and str(item) != ""]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        raise ConverterInputError(f"cannot convert {type(value).__name__} to list[str]")

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "array"
        schema.property["items"] = {"type": "string"}


class ParseList(ValueConverter, SchemaContributor):
    """Привести значение к list[T] через item-конвертер; wire: type=array.

    Принимает list (применяет item.convert к каждому) либо str
    (split по запятой как у ParseCsvList — для env/CLI). Элементы,
    после конвертации равные MISSING, в результат не попадают.
    """

    def __init__(self, item: Converter[Any, Any]) -> None:
        self._item = item

    def _convert_value(self, value: Any) -> list[Any]:
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        elif not isinstance(value, list):
            raise ConverterInputError(f"cannot convert {type(value).__name__} to list")
        result: list[Any] = []
        for i, raw in enumerate(value):
            try:
                converted = self._item.convert(raw)
            except ConverterInputError as exc:
                raise ConverterInputError(f"item[{i}]: {exc}") from exc
            if converted is not MISSING:
                result.append(converted)
        return result

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "array"
        sub = ParamWireSchema()
        if isinstance(self._item, SchemaContributor):
            self._item.contribute(sub)
        if sub.property:
            schema.property["items"] = dict(sub.property)


class ReadTextFile(ValueConverter):
    """Трактует значение как путь к файлу; возвращает его содержимое.

    Не контрибьютит wire-type: итоговый тип определяют соседние
    конвертеры в ChainConverter (например, ParseList после JSON-парсинга).
    Если файла нет или путь не строка — ConverterInputError.
    """

    def __init__(self, *, encoding: str = "utf-8", strip: bool = False) -> None:
        self._encoding = encoding
        self._strip = strip

    def _convert_value(self, value: Any) -> str:
        if not isinstance(value, str):
            raise ConverterInputError(
                f"expected file path (str), got {type(value).__name__}"
            )
        path = Path(value)
        if not path.is_file():
            raise ConverterInputError(f"file not found: {value!r}")
        try:
            text = path.read_text(encoding=self._encoding)
        except (OSError, UnicodeDecodeError) as exc:
            raise ConverterInputError(f"cannot read {value!r}: {exc}") from exc
        return text.strip() if self._strip else text


class ReadJsonFile(ValueConverter):
    """Трактует значение как путь к JSON-файлу; возвращает распарсенное содержимое.

    Не контрибьютит wire-type — см. ReadTextFile. Битый JSON или
    отсутствующий файл — ConverterInputError.
    """

    def __init__(self, *, encoding: str = "utf-8") -> None:
        self._encoding = encoding

    def _convert_value(self, value: Any) -> Any:
        if not isinstance(value, str):
            raise ConverterInputError(
                f"expected file path (str), got {type(value).__name__}"
            )
        path = Path(value)
        if not path.is_file():
            raise ConverterInputError(f"file not found: {value!r}")
        try:
            raw = path.read_text(encoding=self._encoding)
        except (OSError, UnicodeDecodeError) as exc:
            raise ConverterInputError(f"cannot read {value!r}: {exc}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConverterInputError(f"invalid JSON in {value!r}: {exc}") from exc

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
            raise ConverterInputError(f"длина не определена для {type(value).__name__}")
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
            raise ConverterInputError(f"длина не определена для {type(value).__name__}")
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

class IsString(ValueConverter, SchemaContributor):
    """Строго str (без coercion); wire: type=string."""

    def _convert_value(self, value: Any) -> str:
        if not isinstance(value, str):
            raise ConverterInputError(
                f"ожидалась строка, получено {type(value).__name__}"
            )
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "string"


class IsInt(ValueConverter, SchemaContributor):
    """Строго int (bool отвергается); wire: type=integer."""

    def _convert_value(self, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConverterInputError(
                f"ожидалось целое число, получено {type(value).__name__}"
            )
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "integer"


class IsNumber(ValueConverter, SchemaContributor):
    """Строго int или float (bool отвергается); wire: type=number."""

    def _convert_value(self, value: Any) -> int | float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConverterInputError(
                f"ожидалось число, получено {type(value).__name__}"
            )
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "number"


class IsBool(ValueConverter, SchemaContributor):
    """Строго bool; wire: type=boolean."""

    def _convert_value(self, value: Any) -> bool:
        if not isinstance(value, bool):
            raise ConverterInputError(f"ожидался bool, получено {type(value).__name__}")
        return value

    def contribute(self, schema: ParamWireSchema) -> None:
        schema.property["type"] = "boolean"

class MutuallyExclusive(Converter[dict[str, Any], dict[str, Any]]):
    """Максимум один из перечисленных полей задан одновременно."""

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
    """value[first] <= value[second], когда оба поля заданы."""

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
