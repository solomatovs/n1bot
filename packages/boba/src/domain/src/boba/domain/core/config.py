"""Абстракции унифицированной работы с конфигурацией.

Декларативный контракт двух уровней:

- :class:`FieldSpec` описывает одно поле через :class:`ConfigKey`
  (source-agnostic иерархический идентификатор) и
  :class:`~boba.domain.core.patterns.Converter`-цепочку, которая
  превращает сырое значение источника в типизированный результат.
- :class:`ConfigSection` группирует поля одной семантической области
  (LLM, workspaces, конфиг extension'а) и строит из них типизированный DTO.

Конкретный мапинг ``ConfigKey`` на env-имена / TOML-пути / CLI-флаги
лежит на :class:`ConfigSource`-источниках (см. ``boba.infra.config``):
домен ничего не знает про окружение исполнения.

Семантика ``MISSING`` — единая с tools-слоем:
:class:`~boba.domain.core.validators.Required` бросает,
:class:`~boba.domain.core.validators.Default` подставляет значение,
:class:`~boba.domain.core.validators.ValueConverter`-наследники
пропускают MISSING без проверки. Это и есть единственный способ
объявить «обязательное поле» / «поле с дефолтом» — сентинел
``REQUIRED`` и отдельное поле ``default`` у :class:`FieldSpec`
больше не нужны.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TypeVar

from boba.domain.core.patterns import Converter, ConverterInputError, StrId
from boba.domain.core.validators import MISSING

__all__ = [
    "ChainedConfigResolver",
    "ConfigKey",
    "ConfigSection",
    "ConfigSource",
    "FieldSpec",
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


@dataclass(frozen=True)
class FieldSpec(Generic[T]):
    """Декларация одного поля конфига.

    Source-agnostic: знает только свой иерархический ключ
    (:class:`ConfigKey`) и :class:`Converter`-цепочку. Конкретные имена
    в env/TOML/CLI выводят источники.

    ``converter`` — цепочка трансформации сырого значения от источника
    в типизированный ``T``. Типичный шаблон::

        ChainConverter(
            Default(20),     # подставит, если источников молчат
            ParseInt(),      # привести к int (str из env, int из TOML)
            MinValue(1),     # семантическое ограничение
        )

    Реализующие :class:`~boba.domain.core.schema.SchemaContributor`
    шаги цепочки дополняют автогенерируемую operator-доку (когда она
    появится), как и в tool-схемах.

    «Обязательность» поля декларируется в цепочке через
    :class:`~boba.domain.core.validators.Required`; «дефолт» —
    :class:`~boba.domain.core.validators.Default`. Это устраняет
    дублирование сигналов о фолбэке.

    ``description`` — человекочитаемое описание поля для доки оператора.
    """

    key: ConfigKey
    converter: Converter[Any, T]
    description: str = ""

    def read(self, resolver: ChainedConfigResolver) -> T:
        """Прочитать значение через резолвер и прогнать его через цепочку.

        ``None`` от резолвера означает «никто не дал значения» — на
        вход цепочки подаётся :data:`MISSING`. ``Required()`` в цепочке
        бросит :class:`ConverterInputError`; ``Default(...)`` подставит
        своё значение; иначе MISSING пройдёт насквозь — ``read`` вернёт
        его, что для большинства использующих типов — баг (надо явно
        ставить ``Default(...)`` или ``Required()``).
        """
        raw: object = resolver.resolve(self)
        value: Any = MISSING if raw is None else raw
        try:
            return self.converter.convert(value)
        except ConverterInputError as exc:
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
