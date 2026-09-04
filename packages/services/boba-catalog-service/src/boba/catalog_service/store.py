"""Таблицы каталога в Postgres: опубликованные сущности, версии, черновики с
порциями операций, виды с раскладкой и шарингом.

Опубликованное состояние лежит реляционно и читается в CatalogSnapshot;
черновик не материализуется — его снимок сворачивается из порций поверх
снимка базовой версии, который восстанавливается из истории versions.
Публикация применяет свёрнутые операции к таблицам одной транзакцией.

Ошибки:
CatalogStoreError — Postgres недоступен, ответ битый, строки таблиц или
    история версий не складываются в согласованный снимок.
DraftNotFoundError — черновика с таким id нет.
DraftClosedError — черновик уже опубликован или отброшен.
DraftConflictError — expected_seq не равен последнему seq черновика.
DraftStaleError — base_version черновика отстал от опубликованной версии.
ViewNotFoundError — вида с таким id нет.
CatalogOpError — новая порция не применима к снимку черновика.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Any, ClassVar, LiteralString
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from boba.catalog import (
    AcceptAll,
    CatalogDiff,
    CatalogEntity,
    CatalogInvariantError,
    CatalogOp,
    CatalogOpError,
    CatalogSnapshot,
    ChangeStatus,
    EntityKind,
    Flow,
    Layer,
    LoadKind,
    LoadSpec,
    Node,
    ObjectKind,
    ObjectRef,
    ObjectResolver,
    OperationList,
)
from boba.catalog_service.config import CatalogConfig
from boba.catalog_service.records import (
    CatalogStoreError,
    Draft,
    DraftAuthor,
    DraftClosedError,
    DraftConflictError,
    DraftNotFoundError,
    DraftOp,
    DraftStaleError,
    DraftState,
    DraftStatus,
    NodePosition,
    RebaseIssue,
    RebaseResult,
    ShareTargetKind,
    Version,
    View,
    ViewLayout,
    ViewNotFoundError,
    ViewShare,
    ViewSpec,
)
from boba.db.postgres import AsyncPostgresPool, PostgresError, PostgresTable, SqlNames

logger = logging.getLogger(__name__)

__all__ = [
    "CatalogStore",
    "CatalogTable",
]

Cursor = psycopg.AsyncCursor[DictRow]


class LegacyTable(StrEnum):
    """Таблицы первой очереди, которые миграция оставляет на месте."""

    DATASETS = "datasets"
    COLUMNS = "columns"


class LegacyColumn(StrEnum):
    """Колонки первой очереди, которые миграция переименовывает в узловые."""

    FROM_DATASET_ID = "from_dataset_id"
    TO_DATASET_ID = "to_dataset_id"
    DATASET_IDS = "dataset_ids"
    DATASET_ID = "dataset_id"


class LayoutConstraint(StrEnum):
    """Ограничения, которые миграция проверяет по имени и заводит по одному."""

    LAYERS_POSITION = "layers_position_key"
    FLOWS_FROM_DATASET = "flows_from_dataset_id_fkey"
    FLOWS_TO_DATASET = "flows_to_dataset_id_fkey"
    FLOWS_FROM_NODE = "flows_from_node_id_fkey"
    FLOWS_TO_NODE = "flows_to_node_id_fkey"


class CatalogTable(StrEnum):
    """Таблицы схемы каталога."""

    LAYERS = "layers"
    NODES = "nodes"
    LOAD_KINDS = "load_kinds"
    FLOWS = "flows"
    VERSIONS = "versions"
    DRAFTS = "drafts"
    DRAFT_OPS = "draft_ops"
    VIEWS = "views"
    VIEW_LAYOUT = "view_layout"
    VIEW_SHARES = "view_shares"

    @classmethod
    def of_entity(cls, kind: EntityKind) -> CatalogTable:
        if kind is EntityKind.LAYER:
            return cls.LAYERS

        if kind is EntityKind.NODE:
            return cls.NODES

        if kind is EntityKind.LOAD_KIND:
            return cls.LOAD_KINDS

        return cls.FLOWS


class LayersColumn(StrEnum):
    ID = "id"
    NAME = "name"
    POSITION = "position"
    DESCRIPTION = "description"


class NodesColumn(StrEnum):
    ID = "id"
    LAYER_ID = "layer_id"
    SOURCE_ID = "source_id"
    OBJECT_KIND = "object_kind"
    PATH = "path"
    ALIAS = "alias"
    NOTE = "note"


class LoadKindsColumn(StrEnum):
    ID = "id"
    NAME = "name"
    DESCRIPTION = "description"
    FIELDS = "fields"


class FlowsColumn(StrEnum):
    ID = "id"
    FROM_NODE_ID = "from_node_id"
    TO_NODE_ID = "to_node_id"
    LOAD_KIND_ID = "load_kind_id"
    LOAD_VALUES = "load_values"
    DESCRIPTION = "description"


class VersionsColumn(StrEnum):
    NUMBER = "number"
    OPERATIONS = "operations"
    AUTHOR = "author"
    PINS = "pins"
    PUBLISHED_AT = "published_at"


class DraftsColumn(StrEnum):
    ID = "id"
    NAME = "name"
    BASE_VERSION = "base_version"
    STATUS = "status"
    PINS = "pins"
    CREATED_BY = "created_by"
    CREATED_AT = "created_at"


class DraftOpsColumn(StrEnum):
    DRAFT_ID = "draft_id"
    SEQ = "seq"
    AUTHOR = "author"
    OPERATIONS = "operations"
    CREATED_AT = "created_at"


class ViewsColumn(StrEnum):
    ID = "id"
    NAME = "name"
    OWNER_ID = "owner_id"
    NODE_IDS = "node_ids"
    LAYER_IDS = "layer_ids"
    CREATED_AT = "created_at"


class ViewLayoutColumn(StrEnum):
    VIEW_ID = "view_id"
    NODE_ID = "node_id"
    X = "x"
    Y = "y"


class ViewSharesColumn(StrEnum):
    VIEW_ID = "view_id"
    TARGET_KIND = "target_kind"
    TARGET = "target"
    MODE = "mode"


class EntityRows:
    """Соответствие сущностей домена строкам таблиц: колонки, разбор, сборка."""

    UPSERT_ORDER: ClassVar[tuple[EntityKind, ...]] = (
        EntityKind.LAYER,
        EntityKind.LOAD_KIND,
        EntityKind.NODE,
        EntityKind.FLOW,
    )

    @classmethod
    def columns_of(cls, kind: EntityKind) -> tuple[StrEnum, ...]:
        """Колонки, которые пишет публикация."""
        if kind is EntityKind.LAYER:
            return tuple(LayersColumn)

        if kind is EntityKind.NODE:
            return tuple(NodesColumn)

        if kind is EntityKind.LOAD_KIND:
            return tuple(LoadKindsColumn)

        return tuple(FlowsColumn)

    @staticmethod
    def row_of(entity: CatalogEntity) -> dict[str, Any]:
        """Параметры insert по сущности; jsonb и массивы в форме psycopg."""
        if isinstance(entity, Layer):
            return entity.model_dump()

        if isinstance(entity, Node):
            return {
                "id": entity.id,
                "layer_id": entity.layer_id,
                "source_id": entity.ref.source_id,
                "object_kind": entity.ref.kind.value,
                "path": list(entity.ref.path),
                "alias": entity.alias,
                "note": entity.note,
            }

        if isinstance(entity, LoadKind):
            fields = entity.model_dump(mode="json")["fields"]
            return {
                "id": entity.id,
                "name": entity.name,
                "description": entity.description,
                "fields": Jsonb(fields),
            }

        values = entity.load.model_dump(mode="json")["values"]
        return {
            "id": entity.id,
            "from_node_id": entity.from_node_id,
            "to_node_id": entity.to_node_id,
            "load_kind_id": entity.load.kind_id,
            "load_values": Jsonb(values),
            "description": entity.description,
        }

    @staticmethod
    def node_of(row: DictRow) -> Node:
        ref = ObjectRef(
            source_id=row["source_id"],
            kind=ObjectKind(row["object_kind"]),
            path=tuple(row["path"]),
        )
        return Node(
            id=row["id"],
            layer_id=row["layer_id"],
            ref=ref,
            alias=row["alias"],
            note=row["note"],
        )

    @staticmethod
    def flow_of(row: DictRow) -> Flow:
        """Поток из строки flows; значения в форме JSON разбирает модель."""
        load = LoadSpec.model_validate(
            {"kind_id": row["load_kind_id"], "values": row["load_values"]}
        )
        return Flow(
            id=row["id"],
            from_node_id=row["from_node_id"],
            to_node_id=row["to_node_id"],
            load=load,
            description=row["description"],
        )


class CatalogStore(PostgresTable):
    """Хранилище каталога: снимок, версии, черновики, виды.

    Создаётся провайдером рантайма по секции [catalog] и живёт под
    CatalogService, который проверяет права и шлёт события; сам store прав не
    знает.
    """

    PUBLISH_LOCK: ClassVar[str] = "catalog.publish"

    def __init__(
        self, cfg: CatalogConfig, pool: AsyncPostgresPool | None = None
    ) -> None:
        postgres = cfg.connection
        if pool is None:
            postgres = cfg.require_conn()

        super().__init__(postgres, cfg.db_schema, pool)
        self._cfg = cfg

    def _sql(self, text: LiteralString) -> sql.Composed:
        """SQL с именами таблиц по значению enum и колонок с префиксом таблицы:
        l_ layers, n_ nodes, k_ load_kinds, f_ flows, v_ versions, dr_ drafts,
        op_ draft_ops, vw_ views, lay_ view_layout, sh_ view_shares.
        """
        names: dict[str, sql.Composable] = {}
        for table in CatalogTable:
            names[table.value] = self._table(table)

        prefixed: dict[str, type[StrEnum]] = {
            "l": LayersColumn,
            "n": NodesColumn,
            "k": LoadKindsColumn,
            "f": FlowsColumn,
            "v": VersionsColumn,
            "dr": DraftsColumn,
            "op": DraftOpsColumn,
            "vw": ViewsColumn,
            "lay": ViewLayoutColumn,
            "sh": ViewSharesColumn,
        }
        for prefix, columns in prefixed.items():
            for column in columns:
                names[f"{prefix}_{column.value}"] = SqlNames.ident(column)

        return sql.SQL(text).format(**names)

    @asynccontextmanager
    async def _guarded(self, action: str) -> AsyncGenerator[None]:
        """Граница слоя: отказ базы или пула уходит наружу как CatalogStoreError."""
        try:
            yield
        except (psycopg.Error, PostgresError) as exc:
            msg = f"catalog: {action} in schema {self._schema} failed: {exc}"
            raise CatalogStoreError(msg) from exc

    @asynccontextmanager
    async def _transaction(self, action: str) -> AsyncGenerator[Cursor]:
        """Курсор словарей внутри одной транзакции на выделенном соединении."""
        pool = await self._pool()
        async with (
            self._guarded(action),
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            yield cur

    async def setup(self) -> None:
        """Схема и таблицы; повтор безвреден. После create-if-not-exists идут
        идемпотентные миграции раскладки первой очереди: недостающие колонки
        добавляются, колонки наборов переименовываются в узловые, а таблица с
        данными, которые перенести нельзя, останавливает старт понятной ошибкой."""
        async with self._guarded("setup"):
            await self._apply_ddl(self._ddl())
            await self._apply_ddl(self._migrations())

        logger.info("catalog ready: %s", self._cfg.db_schema)

    def _migrations(self) -> tuple[sql.Composed, ...]:
        """Перевод таблиц первой очереди (наборы и колонки) в раскладку процесса
        над источниками. Каждый шаг проверяет, что менять, и ничего не удаляет:
        строки, которые нельзя перевести, останавливают миграцию с текстом,
        что и где лежит."""
        schema = sql.Literal(self._cfg.db_schema)
        return (
            self._sql(
                """
                alter table {layers}
                    add column if not exists {l_position} integer,
                    add column if not exists {l_description} text not null default ''
                """
            ),
            self._sql(
                """
                update {layers} as target
                set {l_position} = numbered.rn - 1
                from (
                    select {l_id} as id, row_number() over (order by {l_name}) as rn
                    from {layers}
                ) as numbered
                where target.{l_id} = numbered.id and target.{l_position} is null
                """
            ),
            self._sql("alter table {layers} alter column {l_position} set not null"),
            self._constraint_ddl(
                CatalogTable.LAYERS,
                LayoutConstraint.LAYERS_POSITION,
                "unique ({l_position}) deferrable initially deferred",
            ),
            self._sql(
                """
                alter table {load_kinds}
                    add column if not exists {k_fields} jsonb not null
                        default '[]'::jsonb
                """
            ),
            self._sql(
                """
                alter table {versions}
                    add column if not exists {v_pins} jsonb not null
                        default '{{}}'::jsonb
                """
            ),
            self._sql(
                """
                alter table {drafts}
                    add column if not exists {dr_pins} jsonb not null
                        default '{{}}'::jsonb
                """
            ),
            self._sql(
                """
                alter table {flows}
                    add column if not exists {f_load_values} jsonb not null
                        default '{{}}'::jsonb
                """
            ),
            self._rename_ddl(
                CatalogTable.FLOWS,
                LegacyColumn.FROM_DATASET_ID,
                FlowsColumn.FROM_NODE_ID,
                "flows reference datasets, nodes over sources cannot be derived "
                "from them",
            ),
            self._rename_ddl(
                CatalogTable.FLOWS,
                LegacyColumn.TO_DATASET_ID,
                FlowsColumn.TO_NODE_ID,
                "flows reference datasets, nodes over sources cannot be derived "
                "from them",
            ),
            sql.SQL(
                """
                alter table {flows}
                    drop constraint if exists {from_dataset},
                    drop constraint if exists {to_dataset}
                """
            ).format(
                flows=self._table(CatalogTable.FLOWS),
                from_dataset=sql.Identifier(LayoutConstraint.FLOWS_FROM_DATASET.value),
                to_dataset=sql.Identifier(LayoutConstraint.FLOWS_TO_DATASET.value),
            ),
            self._constraint_ddl(
                CatalogTable.FLOWS,
                LayoutConstraint.FLOWS_FROM_NODE,
                "foreign key ({f_from_node_id}) references {nodes} ({n_id}) "
                "deferrable initially deferred",
            ),
            self._constraint_ddl(
                CatalogTable.FLOWS,
                LayoutConstraint.FLOWS_TO_NODE,
                "foreign key ({f_to_node_id}) references {nodes} ({n_id}) "
                "deferrable initially deferred",
            ),
            self._rename_ddl(
                CatalogTable.VIEWS,
                LegacyColumn.DATASET_IDS,
                ViewsColumn.NODE_IDS,
                "views filter by dataset ids that have no node counterparts",
            ),
            self._rename_ddl(
                CatalogTable.VIEW_LAYOUT,
                LegacyColumn.DATASET_ID,
                ViewLayoutColumn.NODE_ID,
                "layout positions belong to datasets that have no node counterparts",
            ),
            sql.SQL(
                """
                do $$
                begin
                    if exists (
                        select 1 from information_schema.tables
                        where table_schema = {schema} and table_name = {legacy}
                    ) then
                        raise notice
                            'catalog migration: legacy table %.% is left in place, '
                            'the process keeps nodes over sources instead; '
                            'drop it by hand once it is no longer needed',
                            {schema}, {legacy};
                    end if;
                end $$
                """
            ).format(schema=schema, legacy=sql.Literal(LegacyTable.DATASETS.value)),
        )

    def _constraint_ddl(
        self, table: CatalogTable, name: LayoutConstraint, definition: LiteralString
    ) -> sql.Composed:
        """Ограничение заводится, только если его ещё нет: postgres не знает
        add constraint if not exists."""
        body = self._sql(definition)
        return sql.SQL(
            """
            do $$
            begin
                if not exists (
                    select 1 from pg_constraint
                    where conrelid = {regclass}::regclass and conname = {name}
                ) then
                    alter table {table} add constraint {ident} {body};
                end if;
            end $$
            """
        ).format(
            regclass=sql.Literal(f"{self._cfg.db_schema}.{table.value}"),
            name=sql.Literal(name.value),
            table=self._table(table),
            ident=sql.Identifier(name.value),
            body=body,
        )

    def _rename_ddl(
        self,
        table: CatalogTable,
        legacy: LegacyColumn,
        current: StrEnum,
        reason: LiteralString,
    ) -> sql.Composed:
        """Колонка первой очереди переименовывается в узловую, только пока
        таблица пуста: строки со ссылками на наборы перенести нельзя, и старт
        останавливается с текстом, где они лежат и почему."""
        return sql.SQL(
            """
            do $$
            declare
                stale_rows bigint;
            begin
                if exists (
                    select 1 from information_schema.columns
                    where table_schema = {schema}
                      and table_name = {table_name}
                      and column_name = {legacy_name}
                ) then
                    select count(*) into stale_rows from {table};
                    if stale_rows > 0 then
                        raise exception
                            'catalog migration: %.% has % row(s) with the legacy '
                            'column %: {reason}; move or delete these rows by '
                            'hand before starting',
                            {schema}, {table_name}, stale_rows, {legacy_name};
                    end if;
                    alter table {table} rename column {legacy} to {current};
                end if;
            end $$
            """
        ).format(
            schema=sql.Literal(self._cfg.db_schema),
            table_name=sql.Literal(table.value),
            legacy_name=sql.Literal(legacy.value),
            table=self._table(table),
            legacy=sql.Identifier(legacy.value),
            current=SqlNames.ident(current),
            reason=sql.SQL(reason),
        )

    def _ddl(self) -> tuple[sql.Composed, ...]:
        return (
            self._sql(
                """
                create table if not exists {layers} (
                    {l_id}          uuid primary key,
                    {l_name}        text not null,
                    {l_position}    integer not null,
                    {l_description} text not null default '',
                    unique ({l_name}) deferrable initially deferred,
                    unique ({l_position}) deferrable initially deferred
                )
                """
            ),
            self._sql(
                """
                create table if not exists {load_kinds} (
                    {k_id}          uuid primary key,
                    {k_name}        text not null,
                    {k_description} text not null default '',
                    {k_fields}      jsonb not null default '[]'::jsonb,
                    unique ({k_name}) deferrable initially deferred
                )
                """
            ),
            self._sql(
                """
                create table if not exists {nodes} (
                    {n_id}          uuid primary key,
                    {n_layer_id}    uuid not null references {layers} ({l_id})
                                    deferrable initially deferred,
                    {n_source_id}   uuid not null,
                    {n_object_kind} text not null,
                    {n_path}        text[] not null,
                    {n_alias}       text null,
                    {n_note}        text not null default '',
                    unique ({n_source_id}, {n_object_kind}, {n_path})
                        deferrable initially deferred
                )
                """
            ),
            self._sql(
                """
                create table if not exists {flows} (
                    {f_id}           uuid primary key,
                    {f_from_node_id} uuid not null references {nodes} ({n_id})
                                     deferrable initially deferred,
                    {f_to_node_id}   uuid not null references {nodes} ({n_id})
                                     deferrable initially deferred,
                    {f_load_kind_id} uuid not null references {load_kinds} ({k_id})
                                     deferrable initially deferred,
                    {f_load_values}  jsonb not null default '{{}}'::jsonb,
                    {f_description}  text not null default ''
                )
                """
            ),
            self._sql(
                """
                create table if not exists {versions} (
                    {v_number}       integer primary key,
                    {v_operations}   jsonb not null,
                    {v_author}       jsonb not null,
                    {v_pins}         jsonb not null default '{{}}'::jsonb,
                    {v_published_at} timestamptz not null default now()
                )
                """
            ),
            self._sql(
                """
                create table if not exists {drafts} (
                    {dr_id}           uuid primary key,
                    {dr_name}         text not null,
                    {dr_base_version} integer not null,
                    {dr_status}       text not null,
                    {dr_pins}         jsonb not null default '{{}}'::jsonb,
                    {dr_created_by}   uuid not null,
                    {dr_created_at}   timestamptz not null default now()
                )
                """
            ),
            self._sql(
                """
                create table if not exists {draft_ops} (
                    {op_draft_id}   uuid not null references {drafts} ({dr_id})
                                    on delete cascade,
                    {op_seq}        integer not null,
                    {op_author}     jsonb not null,
                    {op_operations} jsonb not null,
                    {op_created_at} timestamptz not null default now(),
                    primary key ({op_draft_id}, {op_seq})
                )
                """
            ),
            self._sql(
                """
                create table if not exists {views} (
                    {vw_id}         uuid primary key,
                    {vw_name}       text not null,
                    {vw_owner_id}   uuid not null,
                    {vw_node_ids}   uuid[] not null default '{{}}',
                    {vw_layer_ids}  uuid[] not null default '{{}}',
                    {vw_created_at} timestamptz not null default now()
                )
                """
            ),
            self._sql(
                """
                create table if not exists {view_layout} (
                    {lay_view_id} uuid not null references {views} ({vw_id})
                                  on delete cascade,
                    {lay_node_id} uuid not null,
                    {lay_x}       double precision not null,
                    {lay_y}       double precision not null,
                    primary key ({lay_view_id}, {lay_node_id})
                )
                """
            ),
            self._sql(
                """
                create table if not exists {view_shares} (
                    {sh_view_id}     uuid not null references {views} ({vw_id})
                                     on delete cascade,
                    {sh_target_kind} text not null,
                    {sh_target}      text not null,
                    {sh_mode}        text not null,
                    primary key ({sh_view_id}, {sh_target_kind}, {sh_target})
                )
                """
            ),
        )

    async def snapshot(self) -> CatalogSnapshot:
        """Опубликованный снимок из таблиц, проверенный check()."""
        async with self._transaction("snapshot") as cur:
            return await self._read_snapshot(cur)

    async def current_version(self) -> int:
        async with self._transaction("current version") as cur:
            return await self._current_version(cur)

    async def versions(self) -> Sequence[Version]:
        query = self._sql(
            """
            select
                {v_number},
                {v_operations},
                {v_author},
                {v_pins},
                {v_published_at}
            from
                {versions}
            order by
                {v_number}
            """
        )

        async with self._transaction("versions") as cur:
            await cur.execute(query)
            rows = await cur.fetchall()

        versions: list[Version] = []
        for row in rows:
            versions.append(self._version_of(row))

        return versions

    async def snapshot_at(self, version: int) -> CatalogSnapshot:
        """Снимок версии: текущая из таблиц, прошлая — свёрткой истории."""
        async with self._transaction(f"snapshot at version {version}") as cur:
            return await self._snapshot_at(cur, version)

    async def create_draft(
        self, name: str, created_by: UUID, pins: Mapping[UUID, int]
    ) -> Draft:
        """Черновик над текущей опубликованной версией."""
        query = self._sql(
            """
            insert into {drafts} (
                {dr_id},
                {dr_name},
                {dr_base_version},
                {dr_status},
                {dr_pins},
                {dr_created_by}
            )
            values (
                %(id)s,
                %(name)s,
                %(base_version)s,
                %(status)s,
                %(pins)s,
                %(created_by)s
            )
            returning
                {dr_id},
                {dr_name},
                {dr_base_version},
                {dr_status},
                {dr_pins},
                {dr_created_by},
                {dr_created_at}
            """
        )

        async with self._transaction(f"create draft {name!r}") as cur:
            current = await self._current_version(cur)
            params = {
                "id": uuid4(),
                "name": name,
                "base_version": current,
                "status": DraftStatus.OPEN.value,
                "pins": Jsonb(self._pins_json(pins)),
                "created_by": created_by,
            }
            await cur.execute(query, params)
            row = await cur.fetchone()

        if row is None:
            msg = (
                f"catalog: insert into {self._schema}.drafts returned no row "
                f"for draft {name!r}"
            )
            raise CatalogStoreError(msg)

        return self._draft_of(row)

    async def get_draft(self, draft_id: UUID) -> Draft:
        async with self._transaction(f"get draft {draft_id}") as cur:
            return await self._draft(cur, draft_id, lock=False)

    async def list_drafts(self, status: DraftStatus) -> Sequence[Draft]:
        query = self._sql(
            """
            select
                {dr_id},
                {dr_name},
                {dr_base_version},
                {dr_status},
                {dr_pins},
                {dr_created_by},
                {dr_created_at}
            from
                {drafts}
            where
                {dr_status} = %(status)s
            order by
                {dr_created_at},
                {dr_id}
            """
        )

        async with self._transaction(f"list {status.value} drafts") as cur:
            await cur.execute(query, {"status": status.value})
            rows = await cur.fetchall()

        drafts: list[Draft] = []
        for row in rows:
            drafts.append(self._draft_of(row))

        return drafts

    async def discard_draft(self, draft_id: UUID) -> Draft:
        """Черновик отброшен; порции остаются в истории."""
        async with self._transaction(f"discard draft {draft_id}") as cur:
            draft = await self._draft(cur, draft_id, lock=True)
            self._require_open(draft)

            return await self._set_status(cur, draft_id, DraftStatus.DISCARDED)

    async def draft_ops(self, draft_id: UUID) -> Sequence[DraftOp]:
        async with self._transaction(f"ops of draft {draft_id}") as cur:
            await self._draft(cur, draft_id, lock=False)

            return await self._ops_of(cur, draft_id)

    async def draft_state(self, draft_id: UUID) -> DraftState:
        """Снимок черновика поверх базовой версии и diff к ней."""
        async with self._transaction(f"state of draft {draft_id}") as cur:
            draft = await self._draft(cur, draft_id, lock=False)
            base = await self._snapshot_at(cur, draft.base_version)
            ops = await self._ops_of(cur, draft_id)

        folded = self._fold(draft, base, ops)
        diff = CatalogDiff.between(base, folded)

        seq = 0
        if ops:
            seq = ops[-1].seq

        return DraftState(draft=draft, snapshot=folded, diff=diff, seq=seq)

    async def append_ops(
        self,
        draft_id: UUID,
        expected_seq: int,
        author: DraftAuthor,
        ops: OperationList,
        resolver: ObjectResolver,
    ) -> DraftOp:
        """Порция операций; принимается только с актуальным expected_seq, ссылки
        на объекты и колонки проверяются резолвером привязанных версий.

        Ошибки:
        DraftConflictError — expected_seq отстал.
        CatalogOpError — порция не применима к снимку черновика.
        """
        insert = self._sql(
            """
            insert into {draft_ops} (
                {op_draft_id},
                {op_seq},
                {op_author},
                {op_operations}
            )
            values (
                %(draft_id)s,
                %(seq)s,
                %(author)s,
                %(operations)s
            )
            returning
                {op_created_at}
            """
        )

        async with self._transaction(f"append ops to draft {draft_id}") as cur:
            draft = await self._draft(cur, draft_id, lock=True)
            self._require_open(draft)

            current_seq = await self._last_seq(cur, draft_id)
            if expected_seq != current_seq:
                raise DraftConflictError(draft_id, expected_seq, current_seq)

            base = await self._snapshot_at(cur, draft.base_version)
            stored = await self._ops_of(cur, draft_id)
            state = self._fold(draft, base, stored)
            ops.apply(state, resolver)

            seq = current_seq + 1
            params = {
                "draft_id": draft_id,
                "seq": seq,
                "author": Jsonb(author.model_dump(mode="json")),
                "operations": Jsonb(ops.model_dump(mode="json")),
            }
            await cur.execute(insert, params)
            row = await cur.fetchone()

        if row is None:
            msg = (
                f"catalog: insert into {self._schema}.draft_ops returned no row "
                f"for draft {draft_id} seq {seq}"
            )
            raise CatalogStoreError(msg)

        return DraftOp(
            draft_id=draft_id,
            seq=seq,
            author=author,
            operations=ops,
            created_at=row["created_at"],
        )

    async def publish(self, draft_id: UUID, author: DraftAuthor) -> Version:
        """Свёрнутые операции черновика в таблицы и новая версия одной транзакцией.

        Ошибки:
        DraftStaleError — базовая версия черновика отстала, нужен rebase.
        """
        insert_version = self._sql(
            """
            insert into {versions} (
                {v_number},
                {v_operations},
                {v_author},
                {v_pins}
            )
            values (
                %(number)s,
                %(operations)s,
                %(author)s,
                %(pins)s
            )
            returning
                {v_published_at}
            """
        )

        async with self._transaction(f"publish draft {draft_id}") as cur:
            await cur.execute(
                "select pg_advisory_xact_lock(hashtext(%(key)s))",
                {"key": f"{self._schema}.{self.PUBLISH_LOCK}"},
            )

            draft = await self._draft(cur, draft_id, lock=True)
            self._require_open(draft)

            current = await self._current_version(cur)
            if draft.base_version != current:
                raise DraftStaleError(draft_id, draft.base_version, current)

            base = await self._read_snapshot(cur)
            stored = await self._ops_of(cur, draft_id)
            target = self._fold(draft, base, stored)
            await self._write_changes(cur, base, target)

            operations = self._concatenated(stored)
            number = current + 1
            params = {
                "number": number,
                "operations": Jsonb(operations.model_dump(mode="json")),
                "author": Jsonb(author.model_dump(mode="json")),
                "pins": Jsonb(self._pins_json(draft.pins)),
            }
            await cur.execute(insert_version, params)
            row = await cur.fetchone()
            if row is None:
                msg = (
                    f"catalog: insert into {self._schema}.versions returned no "
                    f"row for version {number} of draft {draft_id}"
                )
                raise CatalogStoreError(msg)

            await self._set_status(cur, draft_id, DraftStatus.PUBLISHED)

        return Version(
            number=number,
            operations=operations,
            author=author,
            pins=draft.pins,
            published_at=row["published_at"],
        )

    async def set_pins(self, draft_id: UUID, pins: Mapping[UUID, int]) -> Draft:
        """Привязки черновика к версиям источников: после поднятия до новых."""
        async with self._transaction(f"set pins of draft {draft_id}") as cur:
            draft = await self._draft(cur, draft_id, lock=True)
            self._require_open(draft)
            await cur.execute(
                self._sql(
                    """
                    update {drafts}
                    set {dr_pins} = %(pins)s
                    where {dr_id} = %(draft_id)s
                    """
                ),
                {"draft_id": draft_id, "pins": Jsonb(self._pins_json(pins))},
            )
            return await self._draft(cur, draft_id, lock=False)

    async def rebase(
        self, draft_id: UUID, *, drop_conflicts: bool, resolver: ObjectResolver
    ) -> RebaseResult:
        """Перевод черновика на текущую версию.

        Операции применяются к текущему снимку по одной с проверкой по
        резолверу; не применимые собираются в issues. Без drop_conflicts
        черновик при конфликтах не меняется; с drop_conflicts конфликтные
        операции вычёркиваются из порций, и черновик переводится на текущую
        версию.
        """
        update_ops = self._sql(
            """
            update
                {draft_ops}
            set
                {op_operations} = %(operations)s
            where 1=1
                and {op_draft_id} = %(draft_id)s
                and {op_seq} = %(seq)s
            """
        )
        update_base = self._sql(
            """
            update
                {drafts}
            set
                {dr_base_version} = %(base_version)s
            where
                {dr_id} = %(draft_id)s
            returning
                {dr_id},
                {dr_name},
                {dr_base_version},
                {dr_status},
                {dr_pins},
                {dr_created_by},
                {dr_created_at}
            """
        )

        async with self._transaction(f"rebase draft {draft_id}") as cur:
            draft = await self._draft(cur, draft_id, lock=True)
            self._require_open(draft)

            current = await self._current_version(cur)
            if draft.base_version == current:
                return RebaseResult(draft=draft, issues=())

            base = await self._read_snapshot(cur)
            stored = await self._ops_of(cur, draft_id)

            issues: list[RebaseIssue] = []
            kept: dict[int, list[CatalogOp]] = {}
            state = base
            for portion in stored:
                kept[portion.seq] = []
                for index, op in enumerate(portion.operations.root):
                    try:
                        state = OperationList(root=(op,)).apply(state, resolver)
                    except CatalogOpError as exc:
                        issue = RebaseIssue(
                            seq=portion.seq, index=index, reason=exc.reason
                        )
                        issues.append(issue)
                        continue

                    kept[portion.seq].append(op)

            if issues and not drop_conflicts:
                return RebaseResult(draft=draft, issues=tuple(issues))

            for portion in stored:
                trimmed = OperationList(root=tuple(kept[portion.seq]))
                if len(trimmed.root) == len(portion.operations.root):
                    continue

                params = {
                    "draft_id": draft_id,
                    "seq": portion.seq,
                    "operations": Jsonb(trimmed.model_dump(mode="json")),
                }
                await cur.execute(update_ops, params)

            await cur.execute(
                update_base, {"draft_id": draft_id, "base_version": current}
            )
            row = await cur.fetchone()

        if row is None:
            msg = (
                f"catalog: update of {self._schema}.drafts returned no row for "
                f"draft {draft_id} while rebasing it to version {current}"
            )
            raise CatalogStoreError(msg)

        return RebaseResult(draft=self._draft_of(row), issues=tuple(issues))

    async def create_view(self, owner_id: UUID, spec: ViewSpec) -> View:
        query = self._sql(
            """
            insert into {views} (
                {vw_id},
                {vw_name},
                {vw_owner_id},
                {vw_node_ids},
                {vw_layer_ids}
            )
            values (
                %(id)s,
                %(name)s,
                %(owner_id)s,
                %(node_ids)s,
                %(layer_ids)s
            )
            returning
                {vw_id},
                {vw_name},
                {vw_owner_id},
                {vw_node_ids},
                {vw_layer_ids},
                {vw_created_at}
            """
        )
        params = {
            "id": uuid4(),
            "name": spec.name,
            "owner_id": owner_id,
            "node_ids": list(spec.node_ids),
            "layer_ids": list(spec.layer_ids),
        }

        async with self._transaction(f"create view {spec.name!r}") as cur:
            await cur.execute(query, params)
            row = await cur.fetchone()

        if row is None:
            msg = (
                f"catalog: insert into {self._schema}.views returned no row "
                f"for view {spec.name!r}"
            )
            raise CatalogStoreError(msg)

        return self._view_of(row)

    async def get_view(self, view_id: UUID) -> View:
        async with self._transaction(f"get view {view_id}") as cur:
            return await self._view(cur, view_id)

    async def update_view(self, view_id: UUID, spec: ViewSpec) -> View:
        query = self._sql(
            """
            update
                {views}
            set
                {vw_name} = %(name)s,
                {vw_node_ids} = %(node_ids)s,
                {vw_layer_ids} = %(layer_ids)s
            where
                {vw_id} = %(id)s
            returning
                {vw_id},
                {vw_name},
                {vw_owner_id},
                {vw_node_ids},
                {vw_layer_ids},
                {vw_created_at}
            """
        )
        params = {
            "id": view_id,
            "name": spec.name,
            "node_ids": list(spec.node_ids),
            "layer_ids": list(spec.layer_ids),
        }

        async with self._transaction(f"update view {view_id}") as cur:
            await cur.execute(query, params)
            row = await cur.fetchone()

        if row is None:
            raise ViewNotFoundError(view_id)

        return self._view_of(row)

    async def delete_view(self, view_id: UUID) -> bool:
        """Удаляет вид вместе с раскладкой и шарингом; False — вида не было."""
        query = self._sql(
            """
            delete from
                {views}
            where
                {vw_id} = %(id)s
            """
        )

        async with self._transaction(f"delete view {view_id}") as cur:
            await cur.execute(query, {"id": view_id})
            return cur.rowcount > 0

    async def views_for(
        self, user_id: UUID, roles: Sequence[str], *, everything: bool
    ) -> Sequence[View]:
        """Виды субъекта: все при праве на каталог, иначе свои и расшаренные."""
        access_filter = ""
        if not everything:
            access_filter = """
                and (
                    v.{vw_owner_id} = %(user_id)s
                    or v.{vw_id} in (
                        select
                            s.{sh_view_id}
                        from
                            {view_shares} s
                        where 1=1
                            and (
                                (s.{sh_target_kind} = %(user_kind)s
                                    and s.{sh_target} = %(user_target)s)
                                or (s.{sh_target_kind} = %(role_kind)s
                                    and s.{sh_target} = any(%(roles)s))
                            )
                    )
                )
            """

        query = self._sql(
            """
            select
                v.{vw_id},
                v.{vw_name},
                v.{vw_owner_id},
                v.{vw_node_ids},
                v.{vw_layer_ids},
                v.{vw_created_at}
            from
                {views} v
            where 1=1
                ACCESS_FILTER
            order by
                v.{vw_name},
                v.{vw_id}
            """.replace("ACCESS_FILTER", access_filter)
        )
        params = {
            "user_id": user_id,
            "user_kind": ShareTargetKind.USER.value,
            "user_target": str(user_id),
            "role_kind": ShareTargetKind.ROLE.value,
            "roles": sorted(roles),
        }

        async with self._transaction(f"views for user {user_id}") as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()

        views: list[View] = []
        for row in rows:
            views.append(self._view_of(row))

        return views

    async def layout_of(self, view_id: UUID) -> ViewLayout:
        query = self._sql(
            """
            select
                {lay_node_id},
                {lay_x},
                {lay_y}
            from
                {view_layout}
            where
                {lay_view_id} = %(view_id)s
            order by
                {lay_node_id}
            """
        )

        async with self._transaction(f"layout of view {view_id}") as cur:
            await self._view(cur, view_id)
            await cur.execute(query, {"view_id": view_id})
            rows = await cur.fetchall()

        positions: list[NodePosition] = []
        for row in rows:
            positions.append(
                NodePosition(node_id=row["node_id"], x=row["x"], y=row["y"])
            )

        return ViewLayout(view_id=view_id, positions=tuple(positions))

    async def put_layout(
        self, view_id: UUID, positions: Sequence[NodePosition]
    ) -> ViewLayout:
        """Полная замена раскладки вида."""
        clear = self._sql(
            """
            delete from
                {view_layout}
            where
                {lay_view_id} = %(view_id)s
            """
        )
        insert = self._sql(
            """
            insert into {view_layout} (
                {lay_view_id},
                {lay_node_id},
                {lay_x},
                {lay_y}
            )
            values (
                %(view_id)s,
                %(node_id)s,
                %(x)s,
                %(y)s
            )
            """
        )

        rows: list[dict[str, Any]] = []
        for position in positions:
            rows.append(
                {
                    "view_id": view_id,
                    "node_id": position.node_id,
                    "x": position.x,
                    "y": position.y,
                }
            )

        async with self._transaction(f"put layout of view {view_id}") as cur:
            await self._view(cur, view_id)
            await cur.execute(clear, {"view_id": view_id})
            if rows:
                await cur.executemany(insert, rows)

        return ViewLayout(view_id=view_id, positions=tuple(positions))

    async def shares_of(self, view_id: UUID) -> Sequence[ViewShare]:
        query = self._sql(
            """
            select
                {sh_target_kind},
                {sh_target},
                {sh_mode}
            from
                {view_shares}
            where
                {sh_view_id} = %(view_id)s
            order by
                {sh_target_kind},
                {sh_target}
            """
        )

        async with self._transaction(f"shares of view {view_id}") as cur:
            await self._view(cur, view_id)
            await cur.execute(query, {"view_id": view_id})
            rows = await cur.fetchall()

        shares: list[ViewShare] = []
        for row in rows:
            shares.append(
                ViewShare(
                    kind=ShareTargetKind(row["target_kind"]),
                    target=row["target"],
                    mode=row["mode"],
                )
            )

        return shares

    async def share_view(self, view_id: UUID, share: ViewShare) -> None:
        query = self._sql(
            """
            insert into {view_shares} (
                {sh_view_id},
                {sh_target_kind},
                {sh_target},
                {sh_mode}
            )
            values (
                %(view_id)s,
                %(target_kind)s,
                %(target)s,
                %(mode)s
            )
            on conflict ({sh_view_id}, {sh_target_kind}, {sh_target})
                do update set {sh_mode} = excluded.{sh_mode}
            """
        )
        params = {
            "view_id": view_id,
            "target_kind": share.kind.value,
            "target": share.target,
            "mode": share.mode.value,
        }

        async with self._transaction(f"share view {view_id}") as cur:
            await self._view(cur, view_id)
            await cur.execute(query, params)

    async def unshare_view(self, view_id: UUID, share: ViewShare) -> bool:
        query = self._sql(
            """
            delete from
                {view_shares}
            where 1=1
                and {sh_view_id} = %(view_id)s
                and {sh_target_kind} = %(target_kind)s
                and {sh_target} = %(target)s
            """
        )
        params = {
            "view_id": view_id,
            "target_kind": share.kind.value,
            "target": share.target,
        }

        async with self._transaction(f"unshare view {view_id}") as cur:
            await self._view(cur, view_id)
            await cur.execute(query, params)
            return cur.rowcount > 0

    async def _read_snapshot(self, cur: Cursor) -> CatalogSnapshot:
        """Снимок из таблиц процесса, проверенный check()."""
        layers = await self._rows(
            cur,
            """
            select
                {l_id},
                {l_name},
                {l_position},
                {l_description}
            from
                {layers}
            order by
                {l_position},
                {l_id}
            """,
        )
        nodes = await self._rows(
            cur,
            """
            select
                {n_id},
                {n_layer_id},
                {n_source_id},
                {n_object_kind},
                {n_path},
                {n_alias},
                {n_note}
            from
                {nodes}
            order by
                {n_path},
                {n_id}
            """,
        )
        kinds = await self._rows(
            cur,
            """
            select
                {k_id},
                {k_name},
                {k_description},
                {k_fields}
            from
                {load_kinds}
            order by
                {k_name},
                {k_id}
            """,
        )
        flows = await self._rows(
            cur,
            """
            select
                {f_id},
                {f_from_node_id},
                {f_to_node_id},
                {f_load_kind_id},
                {f_load_values},
                {f_description}
            from
                {flows}
            order by
                {f_id}
            """,
        )

        try:
            return self._assemble(layers, nodes, kinds, flows)
        except ValidationError as exc:
            msg = (
                f"catalog: a row of the entity tables in {self._schema} "
                f"is not a valid entity: {exc}"
            )
            raise CatalogStoreError(msg) from exc
        except CatalogInvariantError as exc:
            msg = f"catalog: entity tables in {self._schema} are inconsistent: {exc}"
            raise CatalogStoreError(msg) from exc

    @staticmethod
    def _assemble(
        layers: Sequence[DictRow],
        nodes: Sequence[DictRow],
        kinds: Sequence[DictRow],
        flows: Sequence[DictRow],
    ) -> CatalogSnapshot:
        layer_table: dict[UUID, Layer] = {}
        for row in layers:
            layer = Layer.model_validate(row)
            layer_table[layer.id] = layer

        node_table: dict[UUID, Node] = {}
        for row in nodes:
            node = EntityRows.node_of(row)
            node_table[node.id] = node

        kind_table: dict[UUID, LoadKind] = {}
        for row in kinds:
            kind = LoadKind.model_validate(row)
            kind_table[kind.id] = kind

        flow_table: dict[UUID, Flow] = {}
        for row in flows:
            flow = EntityRows.flow_of(row)
            flow_table[flow.id] = flow

        snapshot = CatalogSnapshot(
            layers=layer_table,
            nodes=node_table,
            load_kinds=kind_table,
            flows=flow_table,
        )
        snapshot.check()
        return snapshot

    async def _rows(self, cur: Cursor, text: LiteralString) -> Sequence[DictRow]:
        await cur.execute(self._sql(text))
        return await cur.fetchall()

    async def _current_version(self, cur: Cursor) -> int:
        query = self._sql(
            """
            select
                coalesce(max({v_number}), 0) as number
            from
                {versions}
            """
        )
        await cur.execute(query)
        row = await cur.fetchone()
        if row is None:
            msg = (
                f"catalog: reading the latest number from {self._schema}.versions "
                "returned no row, expected one aggregate row"
            )
            raise CatalogStoreError(msg)

        return int(row["number"])

    async def _snapshot_at(self, cur: Cursor, version: int) -> CatalogSnapshot:
        current = await self._current_version(cur)
        if version == current:
            return await self._read_snapshot(cur)

        if version > current:
            msg = (
                f"catalog: version {version} is not published yet, "
                f"the latest in {self._schema}.versions is {current}"
            )
            raise CatalogStoreError(msg)

        query = self._sql(
            """
            select
                {v_number},
                {v_operations},
                {v_author},
                {v_pins},
                {v_published_at}
            from
                {versions}
            where
                {v_number} <= %(version)s
            order by
                {v_number}
            """
        )
        await cur.execute(query, {"version": version})
        rows = await cur.fetchall()

        snapshot = CatalogSnapshot.empty()
        for row in rows:
            stored = self._version_of(row)
            try:
                snapshot = stored.operations.apply(snapshot, AcceptAll())
            except CatalogOpError as exc:
                msg = (
                    f"catalog: operations of version {stored.number} from "
                    f"{self._schema}.versions do not apply on top of the "
                    f"previous versions: {exc}"
                )
                raise CatalogStoreError(msg) from exc

        return snapshot

    async def _draft(self, cur: Cursor, draft_id: UUID, *, lock: bool) -> Draft:
        locking = ""
        if lock:
            locking = "for update"

        query = self._sql(
            """
            select
                {dr_id},
                {dr_name},
                {dr_base_version},
                {dr_status},
                {dr_pins},
                {dr_created_by},
                {dr_created_at}
            from
                {drafts}
            where
                {dr_id} = %(id)s
            LOCKING
            """.replace("LOCKING", locking)
        )
        await cur.execute(query, {"id": draft_id})
        row = await cur.fetchone()
        if row is None:
            raise DraftNotFoundError(draft_id)

        return self._draft_of(row)

    @staticmethod
    def _require_open(draft: Draft) -> None:
        if draft.status is DraftStatus.OPEN:
            return

        raise DraftClosedError(draft.id, draft.status)

    async def _set_status(
        self, cur: Cursor, draft_id: UUID, status: DraftStatus
    ) -> Draft:
        query = self._sql(
            """
            update
                {drafts}
            set
                {dr_status} = %(status)s
            where
                {dr_id} = %(id)s
            returning
                {dr_id},
                {dr_name},
                {dr_base_version},
                {dr_status},
                {dr_created_by},
                {dr_created_at}
            """
        )
        await cur.execute(query, {"id": draft_id, "status": status.value})
        row = await cur.fetchone()
        if row is None:
            raise DraftNotFoundError(draft_id)

        return self._draft_of(row)

    async def _last_seq(self, cur: Cursor, draft_id: UUID) -> int:
        query = self._sql(
            """
            select
                coalesce(max({op_seq}), 0) as seq
            from
                {draft_ops}
            where
                {op_draft_id} = %(draft_id)s
            """
        )
        await cur.execute(query, {"draft_id": draft_id})
        row = await cur.fetchone()
        if row is None:
            msg = (
                f"catalog: reading the last seq from {self._schema}.draft_ops "
                f"for draft {draft_id} returned no row, expected one aggregate row"
            )
            raise CatalogStoreError(msg)

        return int(row["seq"])

    async def _ops_of(self, cur: Cursor, draft_id: UUID) -> Sequence[DraftOp]:
        query = self._sql(
            """
            select
                {op_draft_id},
                {op_seq},
                {op_author},
                {op_operations},
                {op_created_at}
            from
                {draft_ops}
            where
                {op_draft_id} = %(draft_id)s
            order by
                {op_seq}
            """
        )
        await cur.execute(query, {"draft_id": draft_id})
        rows = await cur.fetchall()

        ops: list[DraftOp] = []
        for row in rows:
            try:
                ops.append(DraftOp.model_validate(row))
            except ValidationError as exc:
                msg = (
                    f"catalog: row of {self._schema}.draft_ops for draft "
                    f"{draft_id} seq {row['seq']} is not a valid portion: {exc}"
                )
                raise CatalogStoreError(msg) from exc

        return ops

    @staticmethod
    def _fold(
        draft: Draft, base: CatalogSnapshot, ops: Sequence[DraftOp]
    ) -> CatalogSnapshot:
        """Снимок черновика: порции поверх базы; сохранённые порции обязаны сойтись."""
        state = base
        for portion in ops:
            try:
                state = portion.operations.apply(state, AcceptAll())
            except CatalogOpError as exc:
                msg = (
                    f"catalog: draft {draft.id} seq {portion.seq} no longer applies "
                    f"to version {draft.base_version}: {exc}"
                )
                raise CatalogStoreError(msg) from exc

        return state

    @staticmethod
    def _pins_json(pins: Mapping[UUID, int]) -> dict[str, int]:
        rendered: dict[str, int] = {}
        for source_id, version in pins.items():
            rendered[str(source_id)] = version

        return rendered

    @staticmethod
    def _concatenated(ops: Sequence[DraftOp]) -> OperationList:
        combined: list[CatalogOp] = []
        for portion in ops:
            combined.extend(portion.operations.root)

        return OperationList(root=tuple(combined))

    async def _write_changes(
        self, cur: Cursor, base: CatalogSnapshot, target: CatalogSnapshot
    ) -> None:
        """Таблицы сущностей по diff: upsert добавленных и изменённых, удаление
        пропавших в порядке зависимостей.
        """
        diff = CatalogDiff.between(base, target)

        for kind in EntityRows.UPSERT_ORDER:
            rows: list[dict[str, Any]] = []
            for entity in self._changed(diff, target, kind):
                rows.append(EntityRows.row_of(entity))

            if not rows:
                continue

            await cur.executemany(self._upsert(kind), rows)

        for kind in reversed(EntityRows.UPSERT_ORDER):
            removed: list[UUID] = []
            for entry in diff.entries:
                if entry.ref.kind is not kind:
                    continue

                if entry.status is not ChangeStatus.REMOVED:
                    continue

                removed.append(entry.ref.id)

            if not removed:
                continue

            await cur.execute(self._delete(kind), {"ids": removed})

    @staticmethod
    def _changed(
        diff: CatalogDiff, target: CatalogSnapshot, kind: EntityKind
    ) -> Iterator[CatalogEntity]:
        table = target.table(kind)
        for entry in diff.entries:
            if entry.ref.kind is not kind:
                continue

            if entry.status is ChangeStatus.REMOVED:
                continue

            yield table[entry.ref.id]

    def _upsert(self, kind: EntityKind) -> sql.Composed:
        columns = EntityRows.columns_of(kind)

        idents: list[sql.Composable] = []
        placeholders: list[sql.Composable] = []
        updates: list[sql.Composable] = []
        for column in columns:
            ident = SqlNames.ident(column)
            idents.append(ident)
            placeholders.append(sql.Placeholder(column.value))
            if column.value == "id":
                continue

            updates.append(sql.SQL("{} = excluded.{}").format(ident, ident))

        return sql.SQL(
            """
            insert into {table} ({columns})
            values ({values})
            on conflict ({key}) do update set {updates}
            """
        ).format(
            table=self._table(CatalogTable.of_entity(kind)),
            columns=sql.SQL(", ").join(idents),
            values=sql.SQL(", ").join(placeholders),
            key=sql.Identifier("id"),
            updates=sql.SQL(", ").join(updates),
        )

    def _delete(self, kind: EntityKind) -> sql.Composed:
        return sql.SQL(
            """
            delete from {table} where {key} = any(%(ids)s)
            """
        ).format(
            table=self._table(CatalogTable.of_entity(kind)),
            key=sql.Identifier("id"),
        )

    async def _view(self, cur: Cursor, view_id: UUID) -> View:
        query = self._sql(
            """
            select
                {vw_id},
                {vw_name},
                {vw_owner_id},
                {vw_node_ids},
                {vw_layer_ids},
                {vw_created_at}
            from
                {views}
            where
                {vw_id} = %(id)s
            """
        )
        await cur.execute(query, {"id": view_id})
        row = await cur.fetchone()
        if row is None:
            raise ViewNotFoundError(view_id)

        return self._view_of(row)

    def _draft_of(self, row: DictRow) -> Draft:
        try:
            return Draft.model_validate(row)
        except ValidationError as exc:
            msg = (
                f"catalog: row of {self._schema}.drafts with id {row.get('id')} "
                f"is not a valid draft: {exc}"
            )
            raise CatalogStoreError(msg) from exc

    def _version_of(self, row: DictRow) -> Version:
        try:
            return Version.model_validate(row)
        except ValidationError as exc:
            msg = (
                f"catalog: row of {self._schema}.versions with number "
                f"{row.get('number')} is not a valid version: {exc}"
            )
            raise CatalogStoreError(msg) from exc

    def _view_of(self, row: DictRow) -> View:
        try:
            return View.model_validate(row)
        except ValidationError as exc:
            msg = (
                f"catalog: row of {self._schema}.views with id {row.get('id')} "
                f"is not a valid view: {exc}"
            )
            raise CatalogStoreError(msg) from exc
