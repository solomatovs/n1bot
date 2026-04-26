"""Адресация и резолвинг конфигурационных значений.

Единственная ответственность модуля — связь между декларативным
описанием объекта (см. :mod:`boba.domain.core.declaration`) и плоским
миром источников значений (env, TOML, CLI, …):

- :class:`ConfigKey` — иерархический source-agnostic идентификатор
  поля. Source-реализации (:mod:`boba.config.env`, :mod:`boba.config.toml`)
  превращают его в env-имя / TOML-путь / CLI-флаг.
- :class:`ConfigSource` + :class:`ChainedConfigResolver` — пул источников
  и итерация «первый non-``None`` выигрывает».
- :class:`ConfigSection` — :class:`ObjectSchema` плюс :attr:`namespace`
  для адресации полей. ``build`` собирает полный :class:`ConfigKey` из
  ``namespace`` и ``field.name``, читает значение через резолвер и
  отдаёт его :func:`validate_object` для прогона через цепочку
  конвертеров и фабрику.
- :func:`read_field` — ad-hoc helper для чтения одного поля без
  секции (например, в CLI, который читает ровно один общий с
  расширением ключ).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, ClassVar, Generic, TypeVar

from boba.domain.core.declaration import (
    FieldSpec,
    ObjectSchema,
    validate_object,
)
from boba.domain.core.patterns import ConverterInputError, StrId
from boba.domain.core.validators import MISSING

__all__ = [
    "ChainedConfigResolver",
    "ConfigKey",
    "ConfigSection",
    "ConfigSource",
    "read_field",
]


T = TypeVar("T")


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

    Используется для ad-hoc чтения одного поля вне секции (например,
    из CLI, который читает один общий ключ с расширением). Для
    регулярного чтения секций — :class:`ConfigSection.build`.
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

    Секция = :class:`ObjectSchema` + namespace для адресации полей в
    env/TOML:

    - :attr:`id` — уникальный :class:`StrId` для регистрации в
      :class:`~boba.infra.config.ConfigFactory`;
    - :attr:`namespace` — кортеж частей префикса (``("ext", "chromadb")``,
      ``("app",)``, ...). Полный :class:`ConfigKey` для поля собирается
      как ``ConfigKey(*namespace, field.name)``.
    - :attr:`schema` — :class:`ObjectSchema` секции (поля + invariants +
      factory + description). Сборка DTO в :meth:`build` — generic-через
      :func:`validate_object`.

    Один и тот же примитив используется и для core-секций (LLM,
    workspaces, agent, …), и для extension-секций — последние
    объявляются в pip-installed пакетах и поднимаются через
    entry-point group ``boba.config_sections``.
    """

    id: ClassVar[StrId]
    namespace: ClassVar[tuple[str, ...]]
    schema: ClassVar[ObjectSchema[Any]]

    def build(self, resolver: ChainedConfigResolver) -> T:
        """Прочитать поля :attr:`schema` через резолвер и собрать DTO.

        Адресация: каждое имя поля комбинируется с :attr:`namespace`
        секции в полный :class:`ConfigKey`. ``None`` от резолвера →
        :data:`MISSING` на вход converter-цепочки (как в
        :func:`read_field`).
        """

        def _read_raw(name: str) -> object:
            key = ConfigKey(*self.namespace, name)
            raw = resolver.resolve(key)
            return MISSING if raw is None else raw

        return validate_object(self.schema, _read_raw)
