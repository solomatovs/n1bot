"""Домен каталога данных в Postgres: таблицы снимков подключений (pg_*,
ch_*) и связей (link) в схеме домена, их раскладка по моделям записей и
запись снимка инструментом снятия.

SnapshotTable и SnapshotTables выводят таблицы из объявления частей снимка
(SourceSnapshot.parts): колонки по полям модели, родной ключ, DDL.
CatalogDomain даёт DDL и раскладку всей схемы домена тому, кто её создаёт
(хранилище приложения). SnapshotWriter — путь инструмента снятия: прежние
staging-таблицы подключения дропаются и создаются заново, порции ложатся
строками, перенос в таблицы домена новой версией — одна транзакция.
SnapshotOutcome — итог инструмента, по которому приложение записывает
версию.

Ошибки:
CatalogDomainError — Postgres недоступен или отказал, строки не легли,
    итог инструмента не разобрать.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from enum import StrEnum
from types import NoneType, UnionType
from typing import Any, ClassVar, Union, get_args, get_origin
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from boba.catalog import (
    PartScope,
    SnapshotPart,
    SourceKinds,
    SourceRecord,
    SourceSnapshot,
    TreeScope,
)
from boba.db.postgres.profile import PostgresConfig
from boba.toolkit.result import TextResult, ToolResult
from boba.toolkit.types import SecretRevealing

logger = logging.getLogger(__name__)

__all__ = [
    "CatalogDomain",
    "CatalogDomainError",
    "CatalogStoreConfig",
    "DomainVersions",
    "LinkTable",
    "PartTable",
    "SnapshotColumn",
    "SnapshotKey",
    "SnapshotOutcome",
    "SnapshotReader",
    "SnapshotResultKey",
    "SnapshotTable",
    "SnapshotTables",
    "SnapshotWriter",
    "SqlType",
    "StagingTable",
]


class CatalogDomainError(Exception):
    """Домен каталога недоступен, отказал или итог снятия не разобрать."""


class CatalogStoreConfig(SecretRevealing):
    """Домен каталога глазами инструмента снятия: подключение к Postgres
    каталога и схема домена; секция [catalog], остальные её ключи —
    приложению."""

    model_config = ConfigDict(extra="ignore")

    SECTION: ClassVar[str] = "catalog"

    connection: PostgresConfig = Field(
        description='Postgres-профиль ссылкой: connection = "${postgres}".'
    )
    db_schema: str = Field(
        min_length=1,
        description="Схема postgres домена каталога: pg_*, ch_* и link.",
    )


class SqlType(StrEnum):
    """Типы колонок таблиц снимков."""

    TEXT = "text"
    TEXT_NULL = "text null"
    INT = "integer"
    INT_NULL = "integer null"
    BIGINT = "bigint"
    BIGINT_NULL = "bigint null"
    REAL = "real"
    REAL_NULL = "real null"
    BOOL = "boolean"
    BOOL_NULL = "boolean null"
    TEXTS = "text[]"
    TEXTS_NULL = "text[] null"
    JSONB = "jsonb"
    JSONB_NULL = "jsonb null"

    @property
    def is_json(self) -> bool:
        return self in (SqlType.JSONB, SqlType.JSONB_NULL)

    @classmethod
    def of_annotation(cls, annotation: object) -> SqlType:
        """Тип колонки по аннотации поля модели записи: строки и перечисления
        — text, числа — bigint и real, флаги — boolean, кортежи строк —
        text[], всё остальное (словари, вложенные модели) — jsonb; Optional
        даёт nullable-вариант."""
        nullable = False
        inner = annotation
        origin = get_origin(annotation)
        if origin is Union or origin is UnionType:
            members = [arg for arg in get_args(annotation) if arg is not NoneType]
            nullable = len(members) < len(get_args(annotation))
            if len(members) == 1:
                inner = members[0]
                origin = get_origin(inner)

        base = cls._base_type(inner, origin)
        if not nullable:
            return base

        return cls(f"{base.value} null")

    @classmethod
    def _base_type(cls, inner: object, origin: object) -> SqlType:
        if origin is tuple:
            args = get_args(inner)
            if args and args[0] is str:
                return cls.TEXTS

            return cls.JSONB

        if not isinstance(inner, type):
            return cls.JSONB

        for base, sql_type in cls._scalar_types():
            if issubclass(inner, base):
                return sql_type

        return cls.JSONB

    @classmethod
    def _scalar_types(cls) -> tuple[tuple[type, SqlType], ...]:
        # bool раньше int: bool — подкласс int
        return (
            (bool, cls.BOOL),
            (str, cls.TEXT),
            (int, cls.BIGINT),
            (float, cls.REAL),
        )


class SnapshotColumn:
    """Колонка таблицы снимка: имя поля модели, имя колонки, тип."""

    def __init__(self, field: str, sql_type: SqlType, column: str) -> None:
        self.field = field
        self.sql_type = sql_type
        self.column = column

    @classmethod
    def of_field(cls, model: type[SourceRecord], field: str) -> SnapshotColumn:
        """Колонка по полю модели: тип из аннотации, имя из COLUMN_NAMES."""
        info = model.model_fields[field]
        column = model.COLUMN_NAMES.get(field, field)
        return cls(field, SqlType.of_annotation(info.annotation), column)


class SnapshotKey(StrEnum):
    """Служебные колонки каждой таблицы снимка: суррогатный ключ строки,
    подключение и версия; родной ключ записи с ними образует unique."""

    ID = "id"
    CONNECTION_ID = "connection_id"
    VERSION = "version"

    @classmethod
    def version_columns(cls) -> tuple[SnapshotKey, ...]:
        """Колонки версии — те, что пишутся с каждой строкой части."""
        return (cls.CONNECTION_ID, cls.VERSION)


class LinkTable(StrEnum):
    """Доменная таблица связей между сущностями снимков и её колонки.
    Ссылок на таблицы сущностей нет: сущности разных таблиц в одной колонке,
    их коды — LinkEntity домена. parent_id — связь-родитель (колонка →
    колонка под таблицей → таблица), 0 — корень: null не бывает."""

    TABLE = "link"
    ID = "id"
    FROM_ID = "from_id"
    FROM_ENTITY = "from_entity"
    TO_ID = "to_id"
    TO_ENTITY = "to_entity"
    PARENT_ID = "parent_id"

    @classmethod
    def columns(cls) -> list[str]:
        names: list[str] = []
        for column in cls:
            if column is cls.TABLE:
                continue

            names.append(column.value)

        return names


class PartTable(BaseModel):
    """Часть снимка в домене без моделей: имя части, таблица домена и
    колонки записи по порядку. По ним инструмент снятия и стенд заводят
    staging, льют строки и переносят их в таблицу домена."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    part: str = Field(min_length=1)
    table: str = Field(min_length=1)
    columns: tuple[str, ...] = Field(min_length=1)

    def identifiers(self) -> sql.Composed:
        idents: list[sql.Composable] = []
        for column in self.columns:
            idents.append(sql.Identifier(column))

        return sql.SQL(", ").join(idents)

    def placeholders(self) -> sql.Composed:
        marks: list[sql.Composable] = []
        for column in self.columns:
            marks.append(sql.Placeholder(column))

        return sql.SQL(", ").join(marks)


class StagingTable:
    """Staging-таблицы снятия одного подключения в схеме домена: по таблице
    на часть снимка той же раскладки, что таблица домена, без id,
    connection_id и version. Живут от старта снятия до переноса; прежние
    (после сорвавшегося снятия) дропает следующий старт."""

    PREFIX: ClassVar[str] = "snapshot_"

    @classmethod
    def name_of(cls, connection_id: UUID, part: str) -> str:
        return f"{cls.PREFIX}{connection_id.hex}_{part}"

    @classmethod
    def pattern_of(cls, connection_id: UUID) -> str:
        """Шаблон like для всех staging-таблиц подключения."""
        return f"{cls.PREFIX}{connection_id.hex}\\_%"

    @staticmethod
    async def names_in(
        cur: psycopg.AsyncCursor[Any], schema: str, pattern: str
    ) -> list[str]:
        """Имена таблиц схемы по шаблону like, по алфавиту."""
        await cur.execute(
            "select table_name from information_schema.tables "
            "where table_schema = %(schema)s and table_name like %(pattern)s "
            "order by table_name",
            {"schema": schema, "pattern": pattern},
        )
        rows = await cur.fetchall()

        names: list[str] = []
        for row in rows:
            names.append(str(row[0]))

        return names

    @classmethod
    async def drop_matching(
        cls, cur: psycopg.AsyncCursor[Any], schema: str, pattern: str
    ) -> None:
        """Сносит таблицы схемы по шаблону like."""
        for name in await cls.names_in(cur, schema, pattern):
            await cur.execute(
                sql.SQL("drop table if exists {}").format(sql.Identifier(schema, name))
            )


class SnapshotTable:
    """Таблица одной части снимка: имя, модель, колонки и родной ключ,
    выведенные из объявления части и полей её модели."""

    def __init__(
        self,
        table: str,
        part: SnapshotPart,
        columns: Sequence[SnapshotColumn],
        key: Sequence[str],
    ) -> None:
        self.table = table
        self.part = part
        self.model = part.model
        self.columns = tuple(columns)
        self.key = tuple(key)

    @classmethod
    def of(cls, prefix: str, part: SnapshotPart) -> SnapshotTable:
        columns: list[SnapshotColumn] = []
        for field in part.model.model_fields:
            columns.append(SnapshotColumn.of_field(part.model, field))

        key: list[str] = []
        for field in part.model.KEY:
            key.append(part.model.COLUMN_NAMES.get(field, field))

        return cls(f"{prefix}_{part.name}", part, columns, key)

    def column_of(self, field: str) -> str:
        for column in self.columns:
            if column.field == field:
                return column.column

        known: list[str] = []
        for column in self.columns:
            known.append(column.field)

        msg = (
            f"snapshot table {self.table} has no field {field!r}, known fields: {known}"
        )
        raise CatalogDomainError(msg)

    def part_table(self) -> PartTable:
        """Часть без модели: имя, таблица, колонки записи."""
        names: list[str] = []
        for column in self.columns:
            names.append(column.column)

        return PartTable(part=self.part.name, table=self.table, columns=tuple(names))

    def layout(self) -> list[str]:
        """Колонки таблицы домена: служебные и колонки записи."""
        names: list[str] = list(SnapshotKey)
        for column in self.columns:
            names.append(column.column)

        return names

    def identifiers(self) -> sql.Composed:
        """Колонки записи списком идентификаторов."""
        idents: list[sql.Composable] = []
        for column in self.columns:
            idents.append(sql.Identifier(column.column))

        return sql.SQL(", ").join(idents)

    def rows_of(self, snapshot: SourceSnapshot) -> Iterator[dict[str, Any]]:
        """Строки части снимка в раскладке таблицы, без служебных колонок."""
        for record in snapshot.records_of(self.part.name):
            yield self.row_of(record)

    def insert(self, schema: str) -> sql.Composed:
        """Вставка строки версии подключения именованными плейсхолдерами."""
        idents: list[sql.Composable] = []
        placeholders: list[sql.Composable] = []
        for column in self.layout():
            if column == SnapshotKey.ID.value:
                continue
            idents.append(sql.Identifier(column))
            placeholders.append(sql.Placeholder(column))

        return sql.SQL("insert into {} ({}) values ({})").format(
            sql.Identifier(schema, self.table),
            sql.SQL(", ").join(idents),
            sql.SQL(", ").join(placeholders),
        )

    def native_key(self) -> sql.Composed:
        """Родной ключ строки версии: подключение, версия, ключ записи."""
        key: list[sql.Composable] = []
        for name in SnapshotKey.version_columns():
            key.append(sql.Identifier(name.value))

        for name in self.key:
            key.append(sql.Identifier(name))

        return sql.SQL(", ").join(key)

    def domain_ddl(self, schema: str) -> sql.Composed:
        """Таблица части снимка в схеме домена: bigserial id первичным
        ключом, родной ключ строки версии — unique; внешних ключей нет —
        таблица только на insert, версии живут у приложения."""
        definitions: list[sql.Composable] = [
            sql.SQL("{} bigserial not null primary key").format(
                sql.Identifier(SnapshotKey.ID.value)
            ),
            sql.SQL("{} uuid not null").format(
                sql.Identifier(SnapshotKey.CONNECTION_ID.value)
            ),
            sql.SQL("{} integer not null").format(
                sql.Identifier(SnapshotKey.VERSION.value)
            ),
        ]
        definitions.extend(self._column_definitions())
        definitions.append(
            sql.SQL("constraint {} unique ({})").format(
                sql.Identifier(f"{self.table}_key"), self.native_key()
            )
        )
        return sql.SQL("create table if not exists {} ({})").format(
            sql.Identifier(schema, self.table), sql.SQL(", ").join(definitions)
        )

    def _column_definitions(self) -> list[sql.Composable]:
        definitions: list[sql.Composable] = []
        for column in self.columns:
            definitions.append(
                sql.SQL("{} {}").format(
                    sql.Identifier(column.column), sql.SQL(column.sql_type.value)
                )
            )

        return definitions

    def select(self, schema: str) -> sql.Composed:
        """Колонки записи за одну версию подключения."""
        return sql.SQL(
            "select {} from {} where {} = %(connection_id)s and {} = %(version)s"
        ).format(
            self.identifiers(),
            sql.Identifier(schema, self.table),
            sql.Identifier(SnapshotKey.CONNECTION_ID.value),
            sql.Identifier(SnapshotKey.VERSION.value),
        )

    def select_where(self, schema: str, part_scope: PartScope) -> sql.Composed:
        """Выборка части с равенствами области: колонки по полям записи,
        значения плейсхолдерами w0, w1… по порядку области."""
        query = self.select(schema)
        for index, (field, _value) in enumerate(part_scope.where):
            query = sql.SQL("{} and {} = {}").format(
                query,
                sql.Identifier(self.column_of(field)),
                sql.Placeholder(f"w{index}"),
            )

        return query

    def record_of(self, row: Mapping[str, Any]) -> SourceRecord:
        """Запись из строки выборки.

        Ошибки:
        CatalogDomainError — строка не складывается в модель записи.
        """
        payload: dict[str, Any] = {}
        for column in self.columns:
            payload[column.field] = row[column.column]

        try:
            return self.model.model_validate(payload)
        except ValidationError as exc:
            msg = (
                f"snapshot table {self.table}: row does not form a valid "
                f"{self.model.__name__}: {exc}"
            )
            raise CatalogDomainError(msg) from exc

    def row_of(self, record: SourceRecord) -> dict[str, Any]:
        """Строка записи для вставки: поля по колонкам, json — Jsonb."""
        dumped: dict[str, Any] = record.model_dump(mode="json")
        row: dict[str, Any] = {}
        for column in self.columns:
            value = dumped[column.field]
            if column.sql_type.is_json and value is not None:
                value = Jsonb(value)

            row[column.column] = value

        return row


class SnapshotTables:
    """Таблицы снимков всех видов реестра: по части на таблицу, в порядке
    объявления частей (от родителей к детям)."""

    def __init__(self, kinds: SourceKinds) -> None:
        self._kinds = kinds

    def of_kind(self, kind: str) -> tuple[SnapshotTable, ...]:
        return self.of_snapshot(self._kinds.snapshot_class(kind))

    @staticmethod
    def of_snapshot(snapshot: type[SourceSnapshot]) -> tuple[SnapshotTable, ...]:
        tables: list[SnapshotTable] = []
        for part in snapshot.parts():
            tables.append(SnapshotTable.of(snapshot.TABLE_PREFIX, part))

        return tuple(tables)

    def all(self) -> Iterator[SnapshotTable]:
        for snapshot in self._kinds.registered():
            yield from self.of_snapshot(snapshot)


class CatalogDomain:
    """DDL и раскладка схемы домена: таблицы снимков всех видов и link."""

    def __init__(self, schema: str, tables: SnapshotTables) -> None:
        self._schema = schema
        self._tables = tables

    def ddl(self) -> tuple[sql.Composed, ...]:
        statements: list[sql.Composed] = []
        for spec in self._tables.all():
            statements.append(spec.domain_ddl(self._schema))

        statements.append(self._link_ddl())
        return tuple(statements)

    def layouts(self) -> dict[str, list[str]]:
        layouts: dict[str, list[str]] = {LinkTable.TABLE.value: LinkTable.columns()}
        for spec in self._tables.all():
            layouts[spec.table] = spec.layout()

        return layouts

    def _link_ddl(self) -> sql.Composed:
        return sql.SQL(
            """
            create table if not exists {table} (
                {id}          bigserial not null primary key,
                {from_id}     bigint not null,
                {from_entity} integer not null,
                {to_id}       bigint not null,
                {to_entity}   integer not null,
                {parent_id}   bigint not null default 0,
                constraint link_unq
                    unique ({from_id}, {to_id}, {from_entity}, {to_entity})
            )
            """
        ).format(
            table=sql.Identifier(self._schema, LinkTable.TABLE.value),
            id=sql.Identifier(LinkTable.ID.value),
            from_id=sql.Identifier(LinkTable.FROM_ID.value),
            from_entity=sql.Identifier(LinkTable.FROM_ENTITY.value),
            to_id=sql.Identifier(LinkTable.TO_ID.value),
            to_entity=sql.Identifier(LinkTable.TO_ENTITY.value),
            parent_id=sql.Identifier(LinkTable.PARENT_ID.value),
        )


class SnapshotResultKey(StrEnum):
    """Ключи metadata итога инструмента снятия."""

    DATABASE = "database"
    SCHEMAS = "schemas"
    OBJECTS = "objects"
    VERSION = "version"
    SERVER_VERSION = "server_version"


class SnapshotOutcome(BaseModel):
    """Итог снятия: версия, под которой строки легли в домен, и версия
    сервера источника. Инструмент кладёт его в metadata TextResult,
    приложение читает обратно."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(ge=1)
    server_version: str

    def metadata(self) -> dict[str, str]:
        return {
            SnapshotResultKey.VERSION.value: str(self.version),
            SnapshotResultKey.SERVER_VERSION.value: self.server_version,
        }

    @classmethod
    def of_result(cls, result: ToolResult) -> SnapshotOutcome:
        """Ошибки:
        CatalogDomainError — итог не текстовый или без версии.
        """
        if not isinstance(result, TextResult):
            msg = (
                "snapshot tool result: expected a TextResult with the version "
                f"in metadata, got {type(result).__name__}"
            )
            raise CatalogDomainError(msg)

        raw = {
            "version": result.metadata.get(SnapshotResultKey.VERSION.value),
            "server_version": result.metadata.get(
                SnapshotResultKey.SERVER_VERSION.value
            ),
        }
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            msg = (
                f"snapshot tool result: metadata {dict(result.metadata)!r} carries "
                f"no valid version: {exc}"
            )
            raise CatalogDomainError(msg) from exc


class SnapshotWriter:
    """Запись снимка одного подключения в домен от инструмента снятия.

    open() дропает staging-таблицы подключения от прежнего снятия и создаёт
    новые по образцу таблиц домена (без id, connection_id и version);
    copy_from() перекачивает строки части из источника потоком COPY → COPY,
    stage() кладёт готовые строки; commit() одной транзакцией заводит
    следующий номер версии подключения, переносит staging в таблицы домена
    и дропает staging. Каждый шаг — своя явная транзакция, режим autocommit
    подключения роли не играет. Ошибки Postgres — CatalogDomainError.
    """

    def __init__(
        self,
        conn: psycopg.AsyncConnection[Any],
        schema: str,
        connection_id: UUID,
        parts: Sequence[PartTable],
    ) -> None:
        self._conn = conn
        self._schema = schema
        self._connection_id = connection_id
        self._parts = tuple(parts)

    def _part(self, part: str) -> PartTable:
        for item in self._parts:
            if item.part == part:
                return item

        known: list[str] = []
        for item in self._parts:
            known.append(item.part)

        msg = f"snapshot writer has no part {part!r}, its parts: {known}"
        raise CatalogDomainError(msg)

    def _staging(self, part: PartTable) -> sql.Identifier:
        return sql.Identifier(
            self._schema, StagingTable.name_of(self._connection_id, part.part)
        )

    def _domain(self, part: PartTable) -> sql.Identifier:
        return sql.Identifier(self._schema, part.table)

    async def open(self) -> None:
        """Staging заново: прежние таблицы подключения — долой, новые — по
        образцу таблиц домена."""
        try:
            async with self._conn.transaction(), self._conn.cursor() as cur:
                await self._drop_staging(cur)
                for part in self._parts:
                    staging = self._staging(part)
                    await cur.execute(
                        sql.SQL("create table {} (like {} including defaults)").format(
                            staging, self._domain(part)
                        )
                    )
                    await cur.execute(
                        sql.SQL(
                            "alter table {} drop column {}, drop column {}, "
                            "drop column {}"
                        ).format(
                            staging,
                            sql.Identifier(SnapshotKey.ID.value),
                            sql.Identifier(SnapshotKey.CONNECTION_ID.value),
                            sql.Identifier(SnapshotKey.VERSION.value),
                        )
                    )
        except psycopg.Error as exc:
            msg = (
                f"snapshot of connection {self._connection_id}: preparing staging "
                f"in schema {self._schema} failed: {exc}"
            )
            raise CatalogDomainError(msg) from exc

    async def copy_from(
        self,
        source: psycopg.AsyncConnection[Any],
        part: str,
        select: sql.Composable,
        params: Mapping[str, Any],
    ) -> int:
        """Строки части из источника в её staging потоком: COPY (select) TO
        STDOUT у источника перетекает в COPY … FROM STDIN у домена; сколько
        строк перелилось."""
        spec = self._part(part)
        out = sql.SQL("copy ({}) to stdout").format(select)
        into = sql.SQL("copy {} ({}) from stdin").format(
            self._staging(spec), spec.identifiers()
        )
        try:
            async with (
                self._conn.transaction(),
                source.cursor() as reading,
                self._conn.cursor() as writing,
            ):
                async with (
                    reading.copy(out, params) as outbound,
                    writing.copy(into) as inbound,
                ):
                    async for chunk in outbound:
                        await inbound.write(chunk)

                copied = writing.rowcount
        except psycopg.Error as exc:
            msg = (
                f"snapshot of connection {self._connection_id}: copying part "
                f"{part!r} into staging failed: {exc}"
            )
            raise CatalogDomainError(msg) from exc

        return copied

    async def stage(self, part: str, rows: Sequence[Mapping[str, Any]]) -> int:
        """Готовые строки части (значения по колонкам) в её staging."""
        spec = self._part(part)
        insert = sql.SQL("insert into {} ({}) values ({})").format(
            self._staging(spec), spec.identifiers(), spec.placeholders()
        )
        try:
            async with self._conn.transaction(), self._conn.cursor() as cur:
                await cur.executemany(insert, rows)
        except psycopg.Error as exc:
            msg = (
                f"snapshot of connection {self._connection_id}: staging "
                f"{len(rows)} rows of part {part!r} failed: {exc}"
            )
            raise CatalogDomainError(msg) from exc

        return len(rows)

    async def commit(self) -> int:
        """Staging становится следующей версией подключения в таблицах домена
        одной транзакцией; возвращает номер версии."""
        try:
            async with (
                self._conn.transaction(),
                self._conn.cursor(row_factory=dict_row) as cur,
            ):
                version = await self._next_version(cur)
                for part in self._parts:
                    await cur.execute(
                        sql.SQL(
                            "insert into {} ({}, {}, {}) "
                            "select %(connection_id)s, %(version)s, {} from {}"
                        ).format(
                            self._domain(part),
                            sql.Identifier(SnapshotKey.CONNECTION_ID.value),
                            sql.Identifier(SnapshotKey.VERSION.value),
                            part.identifiers(),
                            part.identifiers(),
                            self._staging(part),
                        ),
                        {"connection_id": self._connection_id, "version": version},
                    )

                await self._drop_staging(cur)
        except psycopg.Error as exc:
            msg = (
                f"snapshot of connection {self._connection_id}: moving staging "
                f"into schema {self._schema} failed: {exc}"
            )
            raise CatalogDomainError(msg) from exc

        logger.info(
            "snapshot of connection %s: version %d written to %s",
            self._connection_id,
            version,
            self._schema,
        )
        return version

    async def _next_version(self, cur: psycopg.AsyncCursor[DictRow]) -> int:
        """Номер за последней версией подключения по корневой части."""
        root = self._parts[0]
        await cur.execute(
            DomainVersions.latest_of(self._schema, root.table),
            {"connection_id": self._connection_id},
        )
        row = await cur.fetchone()
        if row is None:
            msg = (
                f"snapshot of connection {self._connection_id}: the version query "
                f"on {self._schema}.{root.table} returned no row"
            )
            raise CatalogDomainError(msg)

        return int(row["version"]) + 1

    async def _drop_staging(self, cur: psycopg.AsyncCursor[Any]) -> None:
        for part in self._parts:
            await cur.execute(
                sql.SQL("drop table if exists {}").format(self._staging(part))
            )


class SnapshotReader:
    """Чтение снимка версии подключения из таблиц домена: целиком или
    только записи областей дерева."""

    def __init__(self, schema: str, kinds: SourceKinds) -> None:
        self._schema = schema
        self._kinds = kinds
        self._tables = SnapshotTables(kinds)

    async def read(
        self,
        cur: psycopg.AsyncCursor[DictRow],
        connection_id: UUID,
        kind: str,
        version: int,
    ) -> SourceSnapshot:
        """Ошибки:
        CatalogDomainError — строки не складываются в снимок вида.
        """
        fields: dict[str, tuple[SourceRecord, ...]] = {}
        for spec in self._tables.of_kind(kind):
            await cur.execute(
                spec.select(self._schema),
                {"connection_id": connection_id, "version": version},
            )
            rows = await cur.fetchall()
            records: list[SourceRecord] = []
            for row in rows:
                records.append(spec.record_of(row))

            fields[spec.part.name] = tuple(records)

        return self._snapshot(connection_id, kind, version, fields)

    async def read_scoped(
        self,
        cur: psycopg.AsyncCursor[DictRow],
        connection_id: UUID,
        kind: str,
        version: int,
        scope: TreeScope,
    ) -> SourceSnapshot:
        """Снимок из записей областей: по запросу на область, части вне
        областей пусты; повторы одной записи из разных областей схлопываются.

        Ошибки:
        CatalogDomainError — строки не складываются в снимок вида.
        """
        fields: dict[str, tuple[SourceRecord, ...]] = {}
        for spec in self._tables.of_kind(kind):
            seen: set[tuple[str, ...]] = set()
            records: list[SourceRecord] = []
            for part_scope in scope.parts:
                if part_scope.part != spec.part.name:
                    continue

                params = self._scope_params(connection_id, version, part_scope)
                await cur.execute(spec.select_where(self._schema, part_scope), params)
                rows = await cur.fetchall()
                for row in rows:
                    record = spec.record_of(row)
                    if record.key in seen:
                        continue

                    seen.add(record.key)
                    records.append(record)

            fields[spec.part.name] = tuple(records)

        return self._snapshot(connection_id, kind, version, fields)

    @staticmethod
    def _scope_params(
        connection_id: UUID, version: int, part_scope: PartScope
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"connection_id": connection_id, "version": version}
        for index, (_field, value) in enumerate(part_scope.where):
            params[f"w{index}"] = value

        return params

    def _snapshot(
        self,
        connection_id: UUID,
        kind: str,
        version: int,
        fields: dict[str, tuple[SourceRecord, ...]],
    ) -> SourceSnapshot:
        try:
            return self._kinds.snapshot_class(kind).model_validate(fields)
        except ValidationError as exc:
            msg = (
                f"rows of connection {connection_id} version {version} in "
                f"{self._schema} do not form a valid {kind} snapshot: {exc}"
            )
            raise CatalogDomainError(msg) from exc


class DomainVersions:
    """Версии подключения в таблицах домена глазами приложения: последняя
    версия и число объектов версии по семействам снимка."""

    def __init__(self, schema: str, snapshot: type[SourceSnapshot]) -> None:
        self._schema = schema
        self._snapshot = snapshot
        self._specs = SnapshotTables.of_snapshot(snapshot)

    def latest_query(self) -> sql.Composed:
        root = self._specs[0]
        return self.latest_of(self._schema, root.table)

    @staticmethod
    def latest_of(schema: str, table: str) -> sql.Composed:
        """Последняя версия подключения по корневой таблице (0 — версий нет)."""
        return sql.SQL(
            "select coalesce(max({}), 0) as version from {} "
            "where {} = %(connection_id)s"
        ).format(
            sql.Identifier(SnapshotKey.VERSION.value),
            sql.Identifier(schema, table),
            sql.Identifier(SnapshotKey.CONNECTION_ID.value),
        )

    def objects_query(self) -> sql.Composed:
        """Число объектов версии: строки частей семейств одним запросом."""
        counts: list[sql.Composable] = []
        for family in self._snapshot.families():
            counts.append(
                sql.SQL(
                    "(select count(*) from {} where {} = %(connection_id)s "
                    "and {} = %(version)s)"
                ).format(
                    sql.Identifier(self._schema, self._table_of(family.part)),
                    sql.Identifier(SnapshotKey.CONNECTION_ID.value),
                    sql.Identifier(SnapshotKey.VERSION.value),
                )
            )

        return sql.SQL("select {} as objects").format(sql.SQL(" + ").join(counts))

    def _table_of(self, part: str) -> str:
        for spec in self._specs:
            if spec.part.name == part:
                return spec.table

        msg = (
            f"snapshot of a {self._snapshot.source_kind()} source has no part {part!r}"
        )
        raise CatalogDomainError(msg)
