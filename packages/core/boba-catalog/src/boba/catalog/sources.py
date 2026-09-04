"""Источники метаданных: вид источника, адрес объекта, дерево обхода снимка.

Снимок хранится в родной структуре своего вида (модули postgres_snapshot и
clickhouse_snapshot); здесь то, что одинаково для любого вида: как объект
адресуется, как снимок раскрывается в дерево любой глубины и какие записи
считаются объектами.

Ошибки:
CatalogInvariantError — снимок нарушает инварианты: повторы ключей,
    ссылки на несуществующих родителей.
"""

from __future__ import annotations

from abc import abstractmethod
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import Field

from boba.catalog.diff import ChangeStatus
from boba.catalog.model import CatalogInvariantError, CatalogModel

__all__ = [
    "ObjectKind",
    "ObjectRef",
    "SourceKind",
    "SourceObject",
    "SourceRecord",
    "TreeKind",
    "TreeNode",
    "TreeNodes",
]


class SourceKind(StrEnum):
    """Форма снимка: системные каталоги какого сервера описывают источник."""

    POSTGRES = "postgres"
    CLICKHOUSE = "clickhouse"


class ObjectKind(StrEnum):
    """Что адресуется в снимке. Postgres: relation, routine, sequence, type;
    ClickHouse: table, dictionary; database и schema — уровни выше объектов."""

    DATABASE = "database"
    SCHEMA = "schema"
    RELATION = "relation"
    ROUTINE = "routine"
    SEQUENCE = "sequence"
    TYPE = "type"
    TABLE = "table"
    DICTIONARY = "dictionary"


class ObjectRef(CatalogModel):
    """Адрес объекта источника, стабильный между версиями: вид объекта и
    родной путь. Postgres: (database, schema, name), рутина — плюс сигнатура;
    ClickHouse: (database, name)."""

    source_id: UUID
    kind: ObjectKind
    path: tuple[str, ...] = Field(min_length=1)

    def render(self) -> str:
        return "/".join(self.path)


class SourceRecord(CatalogModel):
    """Строка снимка: одна запись одной таблицы хранения. Ключ — родной
    составной ключ записи внутри версии, parent — ключ записи-родителя
    (у базы пустой)."""

    @property
    @abstractmethod
    def key(self) -> tuple[str, ...]: ...

    @property
    @abstractmethod
    def parent(self) -> tuple[str, ...]: ...

    @property
    def label(self) -> str:
        return self.key[-1]


class SourceObject(Protocol):
    """Запись, у которой есть адрес: relation, routine, sequence, type, table,
    dictionary. Database и schema адресуются тоже, но объектами процесса не
    становятся."""

    @property
    def key(self) -> tuple[str, ...]: ...

    @property
    def object_kind(self) -> ObjectKind: ...

    @property
    def label(self) -> str: ...

    @property
    def comment(self) -> str | None: ...


class TreeKind(StrEnum):
    """Что за узел в дереве источника."""

    DATABASE = "database"
    SCHEMA = "schema"
    GROUP = "group"
    OBJECT = "object"


class TreeNode(CatalogModel):
    """Узел дерева источника любой глубины. path — путь узла в дереве (не
    адрес объекта: у групп своя ступень), ref — адрес, если узел — объект."""

    path: tuple[str, ...] = Field(min_length=1)
    label: str
    kind: TreeKind
    children_count: int = Field(ge=0)
    detail: str = ""
    comment: str | None = None
    ref: ObjectRef | None = None
    status: ChangeStatus = ChangeStatus.UNCHANGED


class TreeNodes(Protocol):
    """Раскрытие снимка в дерево: детей узла отдаёт снимок своего вида."""

    def children(self, source_id: UUID, path: Sequence[str]) -> Sequence[TreeNode]: ...


class Keyed:
    """Проверки таблиц снимка: ключи уникальны, у каждой записи есть родитель."""

    @staticmethod
    def keys_of(records: Iterable[SourceRecord]) -> set[tuple[str, ...]]:
        keys: set[tuple[str, ...]] = set()
        for record in records:
            keys.add(record.key)

        return keys

    @staticmethod
    def duplicates(records: Iterable[SourceRecord]) -> Iterator[tuple[str, ...]]:
        keys: list[tuple[str, ...]] = []
        for record in records:
            keys.append(record.key)

        counts = Counter(keys)
        for key, count in counts.items():
            if count == 1:
                continue

            yield key

    @staticmethod
    def require_unique(label: str, records: Iterable[SourceRecord]) -> None:
        violations: list[str] = []
        for key in Keyed.duplicates(records):
            violations.append(f"duplicate {label} {'/'.join(key)}")

        if violations:
            raise CatalogInvariantError(violations)

    @staticmethod
    def require_parents(
        label: str, records: Iterable[SourceRecord], parents: set[tuple[str, ...]]
    ) -> None:
        violations: list[str] = []
        for record in records:
            if record.parent in parents:
                continue

            violations.append(f"{label} {'/'.join(record.key)} has no parent")

        if violations:
            raise CatalogInvariantError(violations)
