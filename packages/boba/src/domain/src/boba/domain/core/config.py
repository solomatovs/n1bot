"""Адресация и резолвинг конфигурационных значений.

Связь между ObjectSchema и плоским миром источников (env/TOML/CLI):

- ConfigKey — иерархический source-agnostic идентификатор поля.
- ConfigSource + ChainedConfigResolver — пул источников, первый
  non-None выигрывает.
- ConfigSection — ObjectSchema + namespace; build() собирает ConfigKey
  из namespace + field.name, читает через резолвер и прогоняет через
  validate_object.
- read_field — ad-hoc helper для одного поля без секции.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from typing import Any, ClassVar, Generic, TypeVar

from boba.domain.core.declaration import (
    FieldConverterError,
    FieldMissingError,
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
    (ConfigSource).

    Примеры::

        ConfigKey("llm", "base_url")
        ConfigKey("ext", "chromadb", "persist_path")
        ConfigKey("foo")                    # top-level поле без секции

    Каждая часть допускает [A-Za-z0-9_].

    Сам ConfigKey не хранится у FieldSpec — поле знает
    только своё локальное имя. Полный ключ собирает ConfigSection
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
    """Источник сырых значений по ConfigKey.

    None = «пропусти меня», не «поле отсутствует» — отсутствие
    выявляется после опроса всех источников.

    Конкретный источник сам решает, как превратить ключ в свою
    плоскую конкретику (env-имя, TOML-путь и т.п.) — этим контракт
    декларации в домене и контракт читателя в инфре полностью
    разделены.

    Опционально источник реализует bind_schema (единоразовое
    уведомление о полном наборе ожидаемых ключей — позволяет построить
    argparse, поймать typo в env/TOML) и describe (operator-readable
    «как задать значение через этот источник»). Оба default — no-op /
    пустая строка, source остаётся pull-only.
    """

    def bind_schema(
        self,
        items: Iterable[tuple[ConfigKey, FieldSpec[Any]]],
    ) -> None:
        """Получить полный набор ожидаемых ключей со схемой полей.

        Вызывается ConfigFactory.build один раз, после регистрации
        всех секций (включая discovery extension'ов) и до первого
        resolve. Default — no-op.
        """
        del items

    def describe(self, key: ConfigKey) -> str:
        """Вернуть operator-readable рецепт «как задать этот ключ
        через этот источник»: ``--ext-chromadb-persist-path``,
        ``env BOBA_EXT_CHROMADB_PERSIST_PATH``, ``[ext.chromadb]
        persist_path в TOML`` и т.п.

        Используется ConfigFactory.format_config_error для сборки
        recipe-сообщения при FieldMissingError. Возврат пустой строки —
        «этот источник не умеет адресовать данный ключ» (тогда не
        попадёт в recipe).
        """
        del key
        return ""

    @abstractmethod
    def resolve(self, key: ConfigKey) -> object | None: ...


class ChainedConfigResolver:
    """Опрашивает источники по порядку; первый non-None выигрывает."""

    def __init__(self, sources: Sequence[ConfigSource]) -> None:
        self._sources = list(sources)

    @property
    def sources(self) -> Sequence[ConfigSource]:
        """Read-only view списка источников. Используется ConfigFactory
        в legacy-режиме для вызова bind_schema на каждом источнике
        после регистрации всех секций.
        """
        return tuple(self._sources)

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
    """Прочитать значение для key через резолвер и прогнать его через
    converter field.

    None от резолвера означает «никто не дал значения» — на вход
    цепочки подаётся MISSING. Required() бросит,
    Default(...) подставит, Nullable(...) отдаст None.

    Используется для ad-hoc чтения одного поля вне секции (например,
    из CLI, который читает один общий ключ с расширением). Для
    регулярного чтения секций — build.
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

    Секция = ObjectSchema + namespace для адресации полей в
    env/TOML:

    - id — уникальный StrId для регистрации в
      ConfigFactory;
    - namespace — кортеж частей префикса (("ext", "chromadb"),
      ("app",), ...). Полный ConfigKey для поля собирается
      как ConfigKey(*namespace, field.name).
    - schema — ObjectSchema секции (поля + invariants +
      factory + description). Сборка DTO в build — generic-через
      validate_object.

    Один и тот же примитив используется и для core-секций (LLM,
    workspaces, agent, …), и для extension-секций — последние
    объявляются в pip-installed пакетах и поднимаются через
    entry-point group boba.config_sections.
    """

    id: ClassVar[StrId]
    namespace: ClassVar[tuple[str, ...]]
    schema: ClassVar[ObjectSchema[Any]]

    def build(self, resolver: ChainedConfigResolver) -> T:
        """Прочитать поля schema через резолвер и собрать DTO.

        Адресация: каждое имя поля комбинируется с namespace
        секции в полный ConfigKey. None от резолвера →
        MISSING на вход converter-цепочки (как в
        read_field).

        Ошибки конвертации обогащаются ConfigKey — declaration.py
        не знает, как адресовать поле во внешнем мире, а секция
        знает (это и есть смысл namespace). После этого верхний
        слой (ConfigFactory) умеет формировать operator-friendly
        recipe «как задать значение».
        """

        def _read_raw(name: str) -> object:
            key = ConfigKey(*self.namespace, name)
            raw = resolver.resolve(key)
            return MISSING if raw is None else raw

        try:
            return validate_object(self.schema, _read_raw)
        except FieldConverterError as exc:
            raise self._attach_key(exc) from exc.__cause__

    def _attach_key(self, exc: FieldConverterError) -> FieldConverterError:
        """Дописать ConfigKey к ошибке валидации поля.

        validate_object не знает namespace — оставляет ``exc.key=None``.
        Секция знает namespace и собирает полный ConfigKey из
        ``namespace + field.name``. Тип ошибки сохраняется (FieldMissingError
        остаётся FieldMissingError) — для верхнего слоя важна возможность
        отличить «значение не задано» от «значение невалидно».
        """
        if exc.key is not None:
            return exc
        full_key = ConfigKey(*self.namespace, exc.field.name)
        if isinstance(exc, FieldMissingError):
            return FieldMissingError(str(exc), field=exc.field, key=full_key)
        return FieldConverterError(str(exc), field=exc.field, key=full_key)
