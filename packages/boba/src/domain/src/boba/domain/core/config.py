"""Абстракции унифицированной работы с конфигурацией.

Декларативный контракт двух уровней:

- :class:`FieldSpec` — самодостаточная декларация одного поля:
  ``name`` + :class:`~boba.domain.core.patterns.Converter`-цепочка +
  ``description``. Не несёт информации о своём «адресе» в глобальном
  namespace — поле умеет лишь сказать, как называется и как
  валидировать значение.
- :class:`ConfigSection` группирует поля одной семантической области.
  Намespace-секции (``("ext", "chromadb")`` и т.п.) — её ``ClassVar``;
  при чтении секция сама собирает полный :class:`ConfigKey` из своего
  namespace и имени поля.

Тот же :class:`FieldSpec` используется для tool-параметров (см.
:attr:`ToolInputSchema.params`) — там адресации нет, но форма
декларации одна и та же. Единый wire-схема-протокол через
:meth:`FieldSpec.build_wire_schema`.

Конкретный мапинг ``ConfigKey`` на env-имена / TOML-пути / CLI-флаги
лежит на :class:`ConfigSource`-источниках (см. ``boba.infra.config``):
домен ничего не знает про окружение исполнения.

Семантика ``MISSING`` — единая с tools-слоем:
:class:`~boba.domain.core.validators.Required` бросает,
:class:`~boba.domain.core.validators.Default` подставляет значение,
:class:`~boba.domain.core.validators.ValueConverter`-наследники
пропускают MISSING без проверки.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TypeVar

from boba.domain.core.patterns import Converter, ConverterInputError, StrId
from boba.domain.core.schema import ParamWireSchema, SchemaContributor
from boba.domain.core.validators import MISSING

__all__ = [
    "ChainedConfigResolver",
    "ConfigKey",
    "ConfigSection",
    "ConfigSource",
    "FieldSpec",
    "read_field",
]


T = TypeVar("T")
U = TypeVar("U")


class ConfigKey:
    """Иерархический source-agnostic идентификатор поля конфига.

    Принимает 1+ строковую часть: первая — логическая секция, последняя —
    имя поля, промежуточные — sub-namespaces (для extension'ов). Конкретный
    мапинг на env-переменную, TOML-путь, CLI-флаг — забота источников
    (:class:`ConfigSource`).

    Примеры::

        ConfigKey("llm", "base_url")
        ConfigKey("ext", "chromadb", "persist_path")
        ConfigKey("foo")                    # top-level поле без секции

    Каждая часть допускает ``[A-Za-z0-9_]``.

    Сам :class:`ConfigKey` не хранится у :class:`FieldSpec` — поле знает
    только своё локальное имя. Полный ключ собирает :class:`ConfigSection`
    при чтении из своего namespace + имени поля.
    """

    _MIN_PARTS: ClassVar[int] = 1

    __slots__ = ("_parts",)

    def __init__(self, *parts: str) -> None:
        if len(parts) < self._MIN_PARTS:
            raise ValueError(
                f"ConfigKey requires at least {self._MIN_PARTS} part; "
                f"got {parts!r}"
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
    """Декларация одного поля.

    Самодостаточно: знает только своё локальное ``name``,
    :class:`Converter`-цепочку и человекочитаемое описание. Не несёт
    адреса в глобальном namespace — это задача владельца (для конфига
    — :class:`ConfigSection`, для tool-схемы — :class:`ToolInputSchema`).

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


class ConfigSource(ABC):
    """Источник сырых значений по :class:`ConfigKey`.

    ``None`` = «пропусти меня», не «поле отсутствует» — отсутствие
    выявляется после опроса всех источников.

    Конкретный источник сам решает, как превратить ключ в свою
    плоскую конкретику (env-имя, TOML-путь и т.п.) — этим контракт
    декларации в домене и контракт читателя в инфре полностью
    разделены.
    """

    @abstractmethod
    def resolve(self, key: ConfigKey) -> object | None: ...


class ChainedConfigResolver:
    """Опрашивает источники по порядку; первый non-``None`` выигрывает."""

    def __init__(self, sources: Sequence[ConfigSource]) -> None:
        self._sources = list(sources)

    def resolve(self, key: ConfigKey) -> object | None:
        for source in self._sources:
            value = source.resolve(key)
            if value is not None:
                return value
        return None


def read_field(
    key: ConfigKey,
    field: FieldSpec[T],
    resolver: ChainedConfigResolver,
) -> T:
    """Прочитать значение для ``key`` через резолвер и прогнать его через
    converter ``field``.

    ``None`` от резолвера означает «никто не дал значения» — на вход
    цепочки подаётся :data:`MISSING`. ``Required()`` бросит,
    ``Default(...)`` подставит, ``Nullable(...)`` отдаст ``None``.
    """
    raw: object | None = resolver.resolve(key)
    value: Any = MISSING if raw is None else raw
    try:
        return field.converter.convert(value)
    except ConverterInputError as exc:
        raise ConverterInputError(
            f"Config field {key!r}: {exc}"
        ) from exc


class ConfigSection(ABC, Generic[T]):
    """Декларация одной секции конфига как самодостаточного модуля.

    Секция объединяет:

    - :attr:`id` — уникальный :class:`StrId` для регистрации в
      :class:`~boba.infra.config.ConfigFactory`;
    - :attr:`namespace` — кортеж частей префикса (``("ext", "chromadb")``,
      ``("app",)``, ...). Полный :class:`ConfigKey` для поля собирается
      как ``ConfigKey(*namespace, field.name)``.
    - :attr:`fields` — все её :class:`FieldSpec`-и (фабрика читает их
      для построения карт source'ов и для интроспекции);
    - :meth:`build` — типизированный сборщик DTO из резолвера.

    Один и тот же примитив используется и для core-секций (LLM,
    workspaces, agent, …), и для extension-секций — последние
    объявляются в pip-installed пакетах и поднимаются через
    entry-point group ``boba.config_sections``.
    """

    id: ClassVar[StrId]
    namespace: ClassVar[tuple[str, ...]]
    fields: ClassVar[Sequence[FieldSpec[Any]]]

    def _read(
        self,
        field: FieldSpec[U],
        resolver: ChainedConfigResolver,
    ) -> U:
        """Прочитать значение поля через резолвер.

        Собирает полный :class:`ConfigKey` из ``namespace`` секции и
        ``field.name``, дальше — стандартный путь
        :func:`read_field`.
        """
        return read_field(
            ConfigKey(*self.namespace, field.name),
            field,
            resolver,
        )

    @abstractmethod
    def build(self, resolver: ChainedConfigResolver) -> T:
        """Прочитать :attr:`fields` через резолвер и собрать DTO."""
        ...
