"""Абстракции унифицированной работы с конфигурацией.

Декларативный контракт двух уровней:

- :class:`FieldSpec` описывает одно поле через :class:`ConfigKey`
  (source-agnostic иерархический идентификатор) и :class:`Converter`-парсер.
- :class:`ConfigSection` группирует поля одной семантической области
  (LLM, workspaces, конфиг extension'а) и строит из них типизированный DTO.

Конкретный мапинг ``ConfigKey`` на env-имена / TOML-пути / CLI-флаги
лежит на :class:`ConfigSource`-источниках (см. ``boba.infra.config``):
домен ничего не знает про окружение исполнения.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Final, Generic, TypeVar

from boba.domain.core.patterns import Converter, ConverterInputError, StrId, Validator
from boba.domain.core.validators import ParamValidationError

__all__ = [
    "REQUIRED",
    "BoolConverter",
    "ChainedConfigResolver",
    "ConfigKey",
    "ConfigSection",
    "ConfigSource",
    "CsvListConverter",
    "FieldSpec",
    "FloatConverter",
    "IntConverter",
    "StrConverter",
]


T = TypeVar("T")


class ConfigKey:
    """Иерархический source-agnostic идентификатор поля конфига.

    Принимает 2+ строковых частей: первая — логическая секция, последняя —
    имя поля, промежуточные — sub-namespaces (для extension'ов). Конкретный
    мапинг на env-переменную, TOML-путь, CLI-флаг — забота источников
    (:class:`ConfigSource`).

    Примеры::

        ConfigKey("llm", "base_url")
        ConfigKey("ext", "chromadb", "persist_path")

    Каждая часть допускает ``[A-Za-z0-9_]``.
    """

    _MIN_PARTS: ClassVar[int] = 2

    __slots__ = ("_parts",)

    def __init__(self, *parts: str) -> None:
        if len(parts) < self._MIN_PARTS:
            raise ValueError(
                f"ConfigKey requires at least {self._MIN_PARTS} parts "
                f"(section + field); got {parts!r}"
            )
        for p in parts:
            if not p:
                raise ValueError(
                    f"ConfigKey part must be non-empty string; got {parts!r}"
                )
            if not p.replace("_", "").isalnum():
                raise ValueError(
                    f"ConfigKey part {p!r} must match [A-Za-z0-9_]; "
                    f"got {parts!r}"
                )
        self._parts = parts

    @property
    def parts(self) -> tuple[str, ...]:
        return self._parts

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ConfigKey) and self._parts == other._parts

    def __hash__(self) -> int:
        return hash(self._parts)

    def __repr__(self) -> str:
        inside = ", ".join(repr(p) for p in self._parts)
        return f"ConfigKey({inside})"


class _Required:
    """Тип-маркер для :data:`REQUIRED`. Singleton — наружу экспортируется
    только готовый инстанс, прямое инстанцирование не предполагается.
    """

    __slots__ = ()
    _instance: ClassVar[_Required | None] = None

    def __new__(cls) -> _Required:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "REQUIRED"


REQUIRED: Final[_Required] = _Required()
"""Sentinel-значение для :attr:`FieldSpec.default`, обозначающее
обязательное поле. Отличается от ``None``: ``None`` — это валидный
nullable-дефолт, ``REQUIRED`` — поле обязано приехать из источника,
иначе :meth:`FieldSpec.read` бросит :class:`ConverterInputError`.
"""


@dataclass(frozen=True)
class FieldSpec(Generic[T]):
    """Декларация одного поля конфига.

    Source-agnostic: знает только свой иерархический ключ
    (:class:`ConfigKey`) и тип (через :class:`Converter`). Конкретные
    имена в env/TOML/CLI выводят источники.

    ``default``:

    - конкретное значение типа ``T`` (включая ``None``, если ``T``
      допускает nullable) — fallback, если ни один источник не дал
      значения;
    - :data:`REQUIRED` — поле обязано приехать из источника; иначе
      :meth:`read` бросит :class:`ConverterInputError`.

    ``validator`` — опциональный пост-конверсионный валидатор
    (:class:`Validator`) для семантических ограничений уже типизированного
    значения: ``OneOf("default")``, ``MinValue(1)``, ``NonEmpty()`` и т.п.
    Применяется **только** к значениям, пришедшим из источников: ``default``
    декларируется программистом и считается доверенным, валидация его не
    касается. Tools и config переиспользуют один и тот же набор валидаторов
    из :mod:`boba.domain.core.validators`. Реализующие
    :class:`~boba.domain.core.schema.SchemaContributor` валидаторы
    дополняют автогенерируемую operator-доку (когда она появится),
    как и в tool-схемах.

    ``description`` — человекочитаемое описание поля для доки оператора.
    Хук на будущее; сейчас не используется потребителями, но фиксируем
    в декларации, чтобы не ломать сигнатуру при добавлении автогенерации.
    """

    key: ConfigKey
    converter: Converter[object, T]
    default: T | _Required
    validator: Validator[T] | None = None
    description: str = ""

    def read(self, resolver: ChainedConfigResolver) -> T:
        value = resolver.resolve(self)
        if value is None:
            if isinstance(self.default, _Required):
                raise ConverterInputError(
                    f"Config field {self.key!r} is required but no "
                    "source provided a value"
                )
            return self.default
        return self._validate(self._convert(value))

    def read_opt(self, resolver: ChainedConfigResolver) -> T | None:
        """Аналогично :meth:`read`, но :data:`REQUIRED`-поле без значения
        возвращает ``None`` вместо исключения. Для не-REQUIRED полей
        семантика идентична :meth:`read`.
        """
        value = resolver.resolve(self)
        if value is None:
            if isinstance(self.default, _Required):
                return None
            return self.default
        return self._validate(self._convert(value))

    def _convert(self, value: object) -> T:
        try:
            return self.converter.convert(value)
        except ConverterInputError as exc:
            raise ConverterInputError(f"Config field {self.key!r}: {exc}") from exc

    def _validate(self, value: T) -> T:
        if self.validator is None:
            return value
        try:
            return self.validator.validate(value)
        except ParamValidationError as exc:
            raise ConverterInputError(
                f"Config field {self.key!r}: {exc}"
            ) from exc


class ConfigSource(ABC):
    """Источник сырых значений по :class:`FieldSpec`.

    ``None`` = «пропусти меня», не «поле отсутствует» — отсутствие
    выявляет :meth:`FieldSpec.read` после опроса всех источников.

    Конкретный источник сам решает, как превратить ``spec.key``
    (:class:`ConfigKey`) в свою плоскую конкретику (env-имя, TOML-путь
    и т.п.) — этим контракт декларации в домене и контракт читателя
    в инфре полностью разделены.
    """

    @abstractmethod
    def resolve(self, spec: FieldSpec[Any]) -> object | None: ...


class ChainedConfigResolver:
    """Опрашивает источники по порядку; первый non-``None`` выигрывает."""

    def __init__(self, sources: Sequence[ConfigSource]) -> None:
        self._sources = list(sources)

    def resolve(self, spec: FieldSpec[Any]) -> object | None:
        for source in self._sources:
            value = source.resolve(spec)
            if value is not None:
                return value
        return None


class ConfigSection(ABC, Generic[T]):
    """Декларация одной секции конфига как самодостаточного модуля.

    Секция объединяет:

    - :attr:`id` — уникальный :class:`StrId` для регистрации в
      :class:`~boba.infra.config.ConfigFactory`;
    - :attr:`fields` — все её :class:`FieldSpec`-и (фабрика читает их
      для построения карт source'ов и для интроспекции);
    - :meth:`build` — типизированный сборщик DTO из резолвера.

    Один и тот же примитив используется и для core-секций (LLM,
    workspaces, agent, …), и для extension-секций — последние
    объявляются в pip-installed пакетах и поднимаются через
    entry-point group ``boba.config_sections``.
    """

    id: ClassVar[StrId]
    fields: ClassVar[Sequence[FieldSpec[Any]]]

    @abstractmethod
    def build(self, resolver: ChainedConfigResolver) -> T:
        """Прочитать :attr:`fields` через резолвер и собрать DTO."""
        ...


class StrConverter(Converter[object, str]):
    def convert(self, value: object) -> str:
        if isinstance(value, str):
            return value
        return str(value)


class IntConverter(Converter[object, int]):
    def convert(self, value: object) -> int:
        # bool — подкласс int в Python; для конфига это почти всегда ошибка.
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


class FloatConverter(Converter[object, float]):
    def convert(self, value: object) -> float:
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


class BoolConverter(Converter[object, bool]):
    _TRUE = frozenset({"true", "1", "yes", "on"})
    _FALSE = frozenset({"false", "0", "no", "off"})

    def convert(self, value: object) -> bool:
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


class CsvListConverter(Converter[object, list[str]]):
    """``list`` из TOML — как есть; ``str`` из env — split по ``,``."""

    def convert(self, value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if item is not None and str(item) != ""]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        raise ConverterInputError(f"cannot convert {type(value).__name__} to list[str]")
