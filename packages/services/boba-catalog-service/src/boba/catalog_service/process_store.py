"""Таблицы процессов в Postgres: процессы, их опубликованные сущности,
версии, черновики с порциями операций, ссылки на просмотр.

Опубликованное состояние каждого процесса лежит реляционно и читается в
CatalogSnapshot; черновик не материализуется — его снимок сворачивается из
порций поверх снимка базовой версии, который восстанавливается из истории
версий процесса. Публикация применяет свёрнутые операции к таблицам одной
транзакцией.

Ошибки:
CatalogStoreError — Postgres недоступен, ответ битый, строки таблиц или
    история версий не складываются в согласованный снимок.
ProcessNotFoundError — процесса с таким id нет.
ProcessNameTakenError — процесс с таким именем уже есть.
DraftNotFoundError — черновика с таким id нет.
DraftClosedError — черновик уже опубликован или отброшен.
DraftConflictError — expected_seq не равен последнему seq черновика.
DraftStaleError — base_version черновика отстал от опубликованной версии.
ShareNotFoundError — ссылки с таким token нет или она отозвана.
CatalogOpError — новая порция не применима к снимку черновика.
UpgradeNotFoundError — запуска upgrade с таким id нет.
UpgradeClosedError — запуск upgrade уже закрыт.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Iterator, Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.errors import UniqueViolation
from psycopg.rows import DictRow
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
    ColumnLink,
    EntityKind,
    Flow,
    Group,
    Node,
    ObjectKind,
    ObjectRef,
    ObjectResolver,
    OperationList,
    Position,
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
    NodeUsage,
    Process,
    ProcessNameTakenError,
    ProcessNotFoundError,
    ProcessSpec,
    RebaseIssue,
    RebaseResult,
    Share,
    ShareNotFoundError,
    SyncStatus,
    Upgrade,
    UpgradeClosedError,
    UpgradeNotFoundError,
    UpgradeRun,
    UpgradeStatus,
    UpgradeTarget,
    Version,
)
from boba.catalog_service.store_base import CatalogStoreBase
from boba.db.postgres import (
    Cursor,
    PgQuery,
    PgQueryBuilder,
    PostgresPool,
    PostgresSchema,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CatalogTable",
    "ProcessStore",
]


class CatalogTable(StrEnum):
    """Таблицы процессов в схеме каталога."""

    PROCESSES = "processes"
    GROUPS = "groups"
    NODES = "nodes"
    FLOWS = "flows"
    VERSIONS = "process_versions"
    DRAFTS = "drafts"
    DRAFT_OPS = "draft_ops"
    SHARES = "shares"
    UPGRADES = "process_upgrades"
    UPGRADE_RUNS = "process_upgrade_runs"

    @classmethod
    def of_entity(cls, kind: EntityKind) -> CatalogTable:
        if kind is EntityKind.GROUP:
            return cls.GROUPS

        if kind is EntityKind.NODE:
            return cls.NODES

        return cls.FLOWS


class ProcessesColumn(StrEnum):
    ID = "id"
    NAME = "name"
    DESCRIPTION = "description"
    OWNER_ID = "owner_id"
    CREATED_AT = "created_at"


class EntityColumn(StrEnum):
    """Служебные колонки таблиц сущностей."""

    ID = "id"
    PROCESS_ID = "process_id"


class GroupsColumn(StrEnum):
    ID = "id"
    PROCESS_ID = "process_id"
    NAME = "name"


class NodesColumn(StrEnum):
    ID = "id"
    PROCESS_ID = "process_id"
    GROUP_ID = "group_id"
    X = "x"
    Y = "y"
    WIDTH = "width"
    CONNECTION_ID = "connection_id"
    OBJECT_KIND = "object_kind"
    PATH = "path"
    ALIAS = "alias"
    NOTE = "note"


class FlowsColumn(StrEnum):
    ID = "id"
    PROCESS_ID = "process_id"
    FROM_NODE_ID = "from_node_id"
    TO_NODE_ID = "to_node_id"
    COLUMNS = "columns"
    DESCRIPTION = "description"


class VersionsColumn(StrEnum):
    PROCESS_ID = "process_id"
    NUMBER = "number"
    OPERATIONS = "operations"
    AUTHOR = "author"
    PINS = "pins"
    PUBLISHED_AT = "published_at"


class DraftsColumn(StrEnum):
    ID = "id"
    PROCESS_ID = "process_id"
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


class SharesColumn(StrEnum):
    TOKEN = "token"  # noqa: S105 — имя колонки, не секрет
    PROCESS_ID = "process_id"
    CREATED_BY = "created_by"
    CREATED_AT = "created_at"
    REVOKED_AT = "revoked_at"


class UpgradesColumn(StrEnum):
    ID = "id"
    RUN_ID = "run_id"
    PROCESS_ID = "process_id"
    DRAFT_ID = "draft_id"
    STATUS = "status"
    PINS_BEFORE = "pins_before"
    PINS_AFTER = "pins_after"
    PROBLEMS = "problems"
    VERSION = "version"
    AUTHOR = "author"
    AT = "at"


class UpgradeRunsColumn(StrEnum):
    ID = "id"
    TARGET = "target"
    PROCESS_ID = "process_id"
    DRAFT_ID = "draft_id"
    STARTED_BY = "started_by"
    STARTED_AT = "started_at"
    FINISHED_AT = "finished_at"
    STATUS = "status"
    TOTAL = "total"
    DONE = "done"
    MOVED = "moved"
    BLOCKED = "blocked"
    ERROR = "error"


class EntityRows:
    """Соответствие сущностей домена строкам таблиц: колонки, разбор, сборка."""

    UPSERT_ORDER: ClassVar[tuple[EntityKind, ...]] = (
        EntityKind.GROUP,
        EntityKind.NODE,
        EntityKind.FLOW,
    )

    def columns_of(self, kind: EntityKind) -> tuple[StrEnum, ...]:
        """Колонки, которые пишет публикация."""
        if kind is EntityKind.GROUP:
            return tuple(GroupsColumn)

        if kind is EntityKind.NODE:
            return tuple(NodesColumn)

        return tuple(FlowsColumn)

    def row_of(self, process_id: UUID, entity: CatalogEntity) -> dict[str, Any]:
        """Параметры insert по сущности; jsonb и массивы в форме psycopg."""
        if isinstance(entity, Group):
            row = entity.model_dump()
            row[EntityColumn.PROCESS_ID.value] = process_id
            return row

        if isinstance(entity, Node):
            x = None
            y = None
            if entity.position is not None:
                x = entity.position.x
                y = entity.position.y

            return {
                "id": entity.id,
                "process_id": process_id,
                "group_id": entity.group_id,
                "x": x,
                "y": y,
                "width": entity.width,
                "connection_id": entity.ref.connection_id,
                "object_kind": entity.ref.kind.value,
                "path": list(entity.ref.path),
                "alias": entity.alias,
                "note": entity.note,
            }

        columns = entity.model_dump(mode="json")["columns"]
        return {
            "id": entity.id,
            "process_id": process_id,
            "from_node_id": entity.from_node_id,
            "to_node_id": entity.to_node_id,
            "columns": Jsonb(columns),
            "description": entity.description,
        }

    def node_of(self, row: Mapping[str, Any]) -> Node:
        ref = ObjectRef(
            connection_id=row["connection_id"],
            kind=ObjectKind(row["object_kind"]),
            path=tuple(row["path"]),
        )
        position = None
        if row["x"] is not None and row["y"] is not None:
            position = Position(x=row["x"], y=row["y"])

        return Node(
            id=row["id"],
            ref=ref,
            position=position,
            width=row["width"],
            group_id=row["group_id"],
            alias=row["alias"],
            note=row["note"],
        )

    def flow_of(self, row: Mapping[str, Any]) -> Flow:
        """Поток из строки flows; пары колонок в форме JSON разбирает модель."""
        links: list[ColumnLink] = []
        for payload in row["columns"]:
            links.append(ColumnLink.model_validate(payload))

        return Flow(
            id=row["id"],
            from_node_id=row["from_node_id"],
            to_node_id=row["to_node_id"],
            columns=tuple(links),
            description=row["description"],
        )


class ProcessStore(CatalogStoreBase):
    """Хранилище процессов: процессы, снимки, версии, черновики, ссылки.

    Создаётся провайдером рантайма по секции [catalog] и живёт под
    CatalogService, который проверяет права и шлёт события; сам store прав не
    знает. Списки колонок строк идут в запросы именами {draft_columns},
    {version_columns} и {share_columns}.
    """

    PUBLISH_LOCK: ClassVar[str] = "catalog.publish"
    TOKEN_BYTES: ClassVar[int] = 18
    LAYOUT: ClassVar[Mapping[CatalogTable, type[StrEnum]]] = {
        CatalogTable.PROCESSES: ProcessesColumn,
        CatalogTable.GROUPS: GroupsColumn,
        CatalogTable.NODES: NodesColumn,
        CatalogTable.FLOWS: FlowsColumn,
        CatalogTable.VERSIONS: VersionsColumn,
        CatalogTable.DRAFTS: DraftsColumn,
        CatalogTable.DRAFT_OPS: DraftOpsColumn,
        CatalogTable.SHARES: SharesColumn,
        CatalogTable.UPGRADES: UpgradesColumn,
        CatalogTable.UPGRADE_RUNS: UpgradeRunsColumn,
    }

    def __init__(self, cfg: CatalogConfig, pool: PostgresPool | None = None) -> None:
        super().__init__(cfg, cfg.app_schema, pool)
        self._entities = EntityRows()

    def _query(self) -> PgQueryBuilder:
        return PgQueryBuilder(
            schema=self._schema.ident,
            draft_columns=self._column_list(DraftsColumn),
            version_columns=self._column_list(VersionsColumn),
            share_columns=self._column_list(SharesColumn),
        )

    async def setup(self) -> None:
        """Схема приложения и таблицы; повтор безвреден. Таблицы выпуска, где
        процессы жили в схеме домена, переезжают на месте; иная раскладка —
        отказ с расхождением колонок."""
        await self._apply_ddl(())
        await self._move_from_domain()
        await self._apply_ddl(self._ddl())
        await self._check_layouts(self._layouts())

        logger.info("catalog processes ready: %s", self.schema)

    async def _move_from_domain(self) -> None:
        """Таблицы процессов, оставшиеся в схеме домена, — в схему приложения;
        пустые дубли в схеме домена сносятся."""
        domain = self._cfg.db_schema
        if domain == self.schema:
            return

        source = PostgresSchema(domain)
        async with self._transaction(
            "move process tables from the domain schema"
        ) as cur:
            for table in CatalogTable:
                await source.move_table(cur.connection, table.value, self._schema)

    def _layouts(self) -> dict[str, list[str]]:
        layouts: dict[str, list[str]] = {}
        for table, columns in self.LAYOUT.items():
            names: list[str] = []
            for column in columns:
                names.append(column.value)

            layouts[table.value] = names

        return layouts

    def _ddl(self) -> tuple[PgQuery, ...]:
        return (
            self._query()
            .add(
                """
                create table if not exists {schema}.processes (
                    id          uuid primary key,
                    name        text not null unique,
                    description text not null default '',
                    owner_id    uuid not null,
                    created_at  timestamptz not null default now()
                )
                """
            )
            .build(),
            self._query()
            .add(
                """
                create table if not exists {schema}.groups (
                    id          uuid primary key,
                    process_id  uuid not null references {schema}.processes (id)
                                on delete cascade,
                    name        text not null,
                    unique (process_id, name) deferrable initially deferred
                )
                """
            )
            .build(),
            self._query()
            .add(
                """
                create table if not exists {schema}.nodes (
                    id            uuid primary key,
                    process_id    uuid not null references {schema}.processes (id)
                                  on delete cascade,
                    group_id      uuid null references {schema}.groups (id)
                                  deferrable initially deferred,
                    x             double precision null,
                    y             double precision null,
                    width         double precision null,
                    connection_id uuid not null,
                    object_kind   text not null,
                    path          text[] not null,
                    alias         text null,
                    note          text not null default '',
                    unique (process_id, connection_id, object_kind, path)
                        deferrable initially deferred
                )
                """
            )
            .build(),
            self._query()
            .add(
                """
                create table if not exists {schema}.flows (
                    id           uuid primary key,
                    process_id   uuid not null references {schema}.processes (id)
                                 on delete cascade,
                    from_node_id uuid not null references {schema}.nodes (id)
                                 deferrable initially deferred,
                    to_node_id   uuid not null references {schema}.nodes (id)
                                 deferrable initially deferred,
                    columns      jsonb not null default '[]'::jsonb,
                    description  text not null default ''
                )
                """
            )
            .build(),
            self._query()
            .add(
                """
                create table if not exists {schema}.process_versions (
                    process_id   uuid not null references {schema}.processes (id)
                                 on delete cascade,
                    number       integer not null,
                    operations   jsonb not null,
                    author       jsonb not null,
                    pins         jsonb not null default '{{}}'::jsonb,
                    published_at timestamptz not null default now(),
                    primary key (process_id, number)
                )
                """
            )
            .build(),
            self._query()
            .add(
                """
                create table if not exists {schema}.drafts (
                    id           uuid primary key,
                    process_id   uuid null references {schema}.processes (id)
                                 on delete cascade,
                    name         text not null,
                    base_version integer not null,
                    status       text not null,
                    pins         jsonb not null default '{{}}'::jsonb,
                    created_by   uuid not null,
                    created_at   timestamptz not null default now()
                )
                """
            )
            .build(),
            self._query()
            .add(
                """
                create table if not exists {schema}.draft_ops (
                    draft_id   uuid not null references {schema}.drafts (id)
                               on delete cascade,
                    seq        integer not null,
                    author     jsonb not null,
                    operations jsonb not null,
                    created_at timestamptz not null default now(),
                    primary key (draft_id, seq)
                )
                """
            )
            .build(),
            self._query()
            .add(
                """
                create table if not exists {schema}.shares (
                    token      text primary key,
                    process_id uuid not null references {schema}.processes (id)
                               on delete cascade,
                    created_by uuid not null,
                    created_at timestamptz not null default now(),
                    revoked_at timestamptz null
                )
                """
            )
            .build(),
            self._query()
            .add(
                """
                create table if not exists {schema}.process_upgrade_runs (
                    id          uuid primary key,
                    target      text not null,
                    process_id  uuid null references {schema}.processes (id)
                                on delete cascade,
                    draft_id    uuid null references {schema}.drafts (id)
                                on delete cascade,
                    started_by  uuid not null,
                    started_at  timestamptz not null default now(),
                    finished_at timestamptz null,
                    status      text not null,
                    total       integer not null default 0,
                    done        integer not null default 0,
                    moved       integer not null default 0,
                    blocked     integer not null default 0,
                    error       text null
                )
                """
            )
            .build(),
            self._query()
            .add(
                """
                create table if not exists {schema}.process_upgrades (
                    id          uuid primary key,
                    run_id      uuid not null
                                references {schema}.process_upgrade_runs (id)
                                on delete cascade,
                    process_id  uuid null references {schema}.processes (id)
                                on delete cascade,
                    draft_id    uuid null references {schema}.drafts (id)
                                on delete cascade,
                    status      text not null,
                    pins_before jsonb not null,
                    pins_after  jsonb not null,
                    problems    jsonb not null,
                    version     integer null,
                    author      jsonb not null,
                    at          timestamptz not null default now()
                )
                """
            )
            .build(),
            self._query()
            .add(
                """
                create index if not exists process_upgrades_process_at
                    on {schema}.process_upgrades (process_id, at desc)
                """
            )
            .build(),
            self._query()
            .add(
                """
                create index if not exists process_upgrades_draft_at
                    on {schema}.process_upgrades (draft_id, at desc)
                """
            )
            .build(),
            *self._migrations(),
        )

    def _migrations(self) -> tuple[PgQuery, ...]:
        """Перевод таблиц прежних выпусков на месте: ширина карточки у узла,
        запуск у итога upgrade (итоги без запуска остаются от выпуска, где
        upgrade был синхронным вызовом — им запуск не нужен)."""
        return (
            self._query()
            .add(
                """
                alter table {schema}.nodes
                    add column if not exists width double precision null
                """
            )
            .build(),
            self._query()
            .add(
                """
                alter table {schema}.process_upgrades
                    add column if not exists run_id uuid null
                        references {schema}.process_upgrade_runs (id) on delete cascade
                """
            )
            .build(),
        )

    # --- процессы ---

    async def create_process(self, spec: ProcessSpec, owner_id: UUID) -> Process:
        """Новый процесс без версий.

        Ошибки:
        ProcessNameTakenError — имя занято.
        """
        process_id = uuid4()
        insert = (
            self._query()
            .add(
                """
                insert into {schema}.processes
                    (id, name, description, owner_id)
                values
                    (%(id)s, %(name)s, %(description)s, %(owner_id)s)
                """,
                id=process_id,
                name=spec.name,
                description=spec.description,
                owner_id=owner_id,
            )
            .build()
        )

        async with self._transaction(f"create process {spec.name!r}") as cur:
            try:
                await cur.execute(insert.text, insert.params)
            except UniqueViolation as exc:
                raise ProcessNameTakenError(spec.name) from exc

            return await self._process(cur, process_id)

    async def get_process(self, process_id: UUID) -> Process:
        async with self._transaction(f"get process {process_id}") as cur:
            return await self._process(cur, process_id)

    async def list_processes(self) -> Sequence[Process]:
        query = self._process_query().add("order by p.name, p.id").build()
        rows = await self._rows(query, "list processes")

        return self._parse_all(Process, rows)

    async def update_process(self, process_id: UUID, spec: ProcessSpec) -> Process:
        """Имя и описание процесса.

        Ошибки:
        ProcessNameTakenError — имя занято другим процессом.
        """
        update = (
            self._query()
            .add(
                """
                update {schema}.processes
                set name = %(name)s, description = %(description)s
                where id = %(id)s
                """,
                id=process_id,
                name=spec.name,
                description=spec.description,
            )
            .build()
        )

        async with self._transaction(f"update process {process_id}") as cur:
            await self._process(cur, process_id)
            try:
                await cur.execute(update.text, update.params)
            except UniqueViolation as exc:
                raise ProcessNameTakenError(spec.name) from exc

            return await self._process(cur, process_id)

    async def delete_process(self, process_id: UUID) -> bool:
        """Процесс со всем содержимым; False — процесса не было."""
        query = (
            self._query()
            .add("delete from {schema}.processes where id = %(id)s", id=process_id)
            .build()
        )
        removed = await self._execute(query, f"delete process {process_id}")

        return removed > 0

    async def usage_of_connection(self, connection_id: UUID) -> Sequence[NodeUsage]:
        """Опубликованные узлы над объектами подключения по процессам."""
        query = (
            self._query()
            .add(
                """
                select
                    p.id as process_id,
                    p.name as process_name,
                    count(*) as nodes
                from
                    {schema}.nodes n
                    join {schema}.processes p on p.id = n.process_id
                where
                    n.connection_id = %(connection_id)s
                group by
                    p.id, p.name
                order by
                    p.name
                """,
                connection_id=connection_id,
            )
            .build()
        )
        rows = await self._rows(query, f"usage of connection {connection_id}")

        return self._parse_all(NodeUsage, rows)

    # --- снимок и версии ---

    async def snapshot(self, process_id: UUID) -> CatalogSnapshot:
        """Опубликованный снимок процесса из таблиц, проверенный check()."""
        async with self._transaction(f"snapshot of process {process_id}") as cur:
            await self._process(cur, process_id)
            return await self._read_snapshot(cur, process_id)

    async def current_version(self, process_id: UUID) -> int:
        async with self._transaction(f"current version of {process_id}") as cur:
            await self._process(cur, process_id)
            return await self._current_version(cur, process_id)

    async def versions(self, process_id: UUID) -> Sequence[Version]:
        query = (
            self._query()
            .add(
                """
                select
                    {version_columns}
                from
                    {schema}.process_versions
                where
                    process_id = %(process_id)s
                order by
                    number
                """,
                process_id=process_id,
            )
            .build()
        )

        async with self._transaction(f"versions of process {process_id}") as cur:
            await self._process(cur, process_id)
            await cur.execute(query.text, query.params)
            rows = await cur.fetchall()

        return self._parse_all(Version, rows)

    async def snapshot_at(self, process_id: UUID, version: int) -> CatalogSnapshot:
        """Снимок версии: текущая из таблиц, прошлая — свёрткой истории."""
        action = f"snapshot of process {process_id} at version {version}"
        async with self._transaction(action) as cur:
            await self._process(cur, process_id)
            return await self._snapshot_at(cur, process_id, version)

    # --- черновики ---

    async def create_draft(
        self,
        process_id: UUID | None,
        name: str,
        created_by: UUID,
        pins: Mapping[UUID, int],
    ) -> Draft:
        """Черновик над текущей опубликованной версией процесса; без процесса —
        черновик нового процесса над пустым снимком, имя станет именем процесса
        при публикации."""
        async with self._transaction(f"create draft {name!r}") as cur:
            current = 0
            if process_id is not None:
                await self._process(cur, process_id)
                current = await self._current_version(cur, process_id)

            insert = (
                self._query()
                .add(
                    """
                    insert into {schema}.drafts (
                        id,
                        process_id,
                        name,
                        base_version,
                        status,
                        pins,
                        created_by
                    )
                    values (
                        %(id)s,
                        %(process_id)s,
                        %(name)s,
                        %(base_version)s,
                        %(status)s,
                        %(pins)s,
                        %(created_by)s
                    )
                    returning
                        {draft_columns}
                    """,
                    id=uuid4(),
                    process_id=process_id,
                    name=name,
                    base_version=current,
                    status=DraftStatus.OPEN.value,
                    pins=Jsonb(self._pins_json(pins)),
                    created_by=created_by,
                )
                .build()
            )
            await cur.execute(insert.text, insert.params)
            row = self._returning(
                await cur.fetchone(), f"insert into drafts for draft {name!r}"
            )

        return self._parse(Draft, row)

    async def get_draft(self, draft_id: UUID) -> Draft:
        async with self._transaction(f"get draft {draft_id}") as cur:
            return await self._draft(cur, draft_id, lock=False)

    async def open_drafts(self) -> Sequence[Draft]:
        """Открытые черновики всех процессов: для проверки, где стоит подключение."""
        query = (
            self._draft_query()
            .add("where status = %(status)s", status=DraftStatus.OPEN.value)
            .add("order by created_at, id")
            .build()
        )
        rows = await self._rows(query, "list open drafts")

        return self._parse_all(Draft, rows)

    async def drafts_of_author(self, created_by: UUID) -> Sequence[Draft]:
        """Открытые черновики автора по всем процессам и без процесса: для
        плоского списка панели."""
        query = (
            self._draft_query()
            .add(
                "where status = %(status)s and created_by = %(created_by)s",
                status=DraftStatus.OPEN.value,
                created_by=created_by,
            )
            .add("order by created_at, id")
            .build()
        )
        rows = await self._rows(query, f"list drafts of {created_by}")

        return self._parse_all(Draft, rows)

    async def rename_draft(self, draft_id: UUID, name: str) -> Draft:
        """Новое имя открытого черновика."""
        update = (
            self._query()
            .add(
                """
                update {schema}.drafts
                set name = %(name)s
                where id = %(draft_id)s
                returning
                    {draft_columns}
                """,
                draft_id=draft_id,
                name=name,
            )
            .build()
        )

        async with self._transaction(f"rename draft {draft_id}") as cur:
            draft = await self._draft(cur, draft_id, lock=True)
            self._require_open(draft)
            await cur.execute(update.text, update.params)
            row = self._returning(
                await cur.fetchone(),
                f"update of drafts for draft {draft_id} while renaming it to {name!r}",
            )

        return self._parse(Draft, row)

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
            base = await self._base_of(cur, draft)
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
        async with self._transaction(f"append ops to draft {draft_id}") as cur:
            draft = await self._draft(cur, draft_id, lock=True)
            self._require_open(draft)

            current_seq = await self._last_seq(cur, draft_id)
            if expected_seq != current_seq:
                raise DraftConflictError(draft_id, expected_seq, current_seq)

            base = await self._base_of(cur, draft)
            stored = await self._ops_of(cur, draft_id)
            state = self._fold(draft, base, stored)
            ops.apply(state, resolver)

            seq = current_seq + 1
            insert = (
                self._query()
                .add(
                    """
                    insert into {schema}.draft_ops (
                        draft_id,
                        seq,
                        author,
                        operations
                    )
                    values (
                        %(draft_id)s,
                        %(seq)s,
                        %(author)s,
                        %(operations)s
                    )
                    returning
                        created_at
                    """,
                    draft_id=draft_id,
                    seq=seq,
                    author=Jsonb(author.model_dump(mode="json")),
                    operations=Jsonb(ops.model_dump(mode="json")),
                )
                .build()
            )
            await cur.execute(insert.text, insert.params)
            row = self._returning(
                await cur.fetchone(),
                f"insert into draft_ops for draft {draft_id} seq {seq}",
            )

        return DraftOp(
            draft_id=draft_id,
            seq=seq,
            author=author,
            operations=ops,
            created_at=row["created_at"],
        )

    async def publish(self, draft_id: UUID, author: DraftAuthor) -> Version:
        """Свёрнутые операции черновика в таблицы и новая версия процесса
        одной транзакцией; у черновика без процесса сначала создаётся процесс
        с именем черновика, автор черновика — его владелец.

        Ошибки:
        DraftStaleError — базовая версия черновика отстала, нужен rebase.
        ProcessNameTakenError — имя черновика нового процесса уже занято.
        """
        async with self._transaction(f"publish draft {draft_id}") as cur:
            draft = await self._draft(cur, draft_id, lock=True)
            self._require_open(draft)
            process_id = draft.process_id
            if process_id is None:
                process_id = await self._attach_process(cur, draft)

            await self._lock(cur, self.PUBLISH_LOCK, process_id)

            current = await self._current_version(cur, process_id)
            if draft.base_version != current:
                raise DraftStaleError(draft_id, draft.base_version, current)

            base = await self._read_snapshot(cur, process_id)
            stored = await self._ops_of(cur, draft_id)
            target = self._fold(draft, base, stored)
            await self._write_changes(cur, process_id, base, target)

            version = await self._insert_version(
                cur,
                process_id,
                current + 1,
                self._concatenated(stored),
                author,
                draft.pins,
            )
            await self._set_status(cur, draft_id, DraftStatus.PUBLISHED)

        return version

    async def publish_pins(
        self, process_id: UUID, pins: Mapping[UUID, int], author: DraftAuthor
    ) -> Version:
        """Новая версия процесса с теми же узлами и потоками и новыми
        привязками — итог upgrade; операций у версии нет.

        Ошибки:
        ProcessNotFoundError — процесса нет.
        """
        async with self._transaction(f"publish pins of process {process_id}") as cur:
            await self._process(cur, process_id)
            await self._lock(cur, self.PUBLISH_LOCK, process_id)
            number = await self._current_version(cur, process_id) + 1
            return await self._insert_version(
                cur, process_id, number, OperationList(root=()), author, pins
            )

    async def _insert_version(  # noqa: PLR0913 — версия собирается из своих полей
        self,
        cur: Cursor,
        process_id: UUID,
        number: int,
        operations: OperationList,
        author: DraftAuthor,
        pins: Mapping[UUID, int],
    ) -> Version:
        """Строка новой версии процесса в открытой транзакции."""
        insert = (
            self._query()
            .add(
                """
                insert into {schema}.process_versions (
                    process_id, number, operations, author, pins
                )
                values (
                    %(process_id)s, %(number)s, %(operations)s, %(author)s, %(pins)s
                )
                returning {version_columns}
                """,
                process_id=process_id,
                number=number,
                operations=Jsonb(operations.model_dump(mode="json")),
                author=Jsonb(author.model_dump(mode="json")),
                pins=Jsonb(self._pins_json(pins)),
            )
            .build()
        )
        await cur.execute(insert.text, insert.params)
        row = self._returning(
            await cur.fetchone(), f"insert of version {number} of process {process_id}"
        )

        return self._parse(Version, row)

    async def record_upgrade(self, upgrade: Upgrade) -> Upgrade:
        """Итог upgrade в историю: последняя запись по процессу (черновику)
        показывается в списке и на странице."""
        problems: list[dict[str, Any]] = []
        for problem in upgrade.problems:
            problems.append(problem.model_dump(mode="json"))

        insert = (
            self._query()
            .add(
                """
                insert into {schema}.process_upgrades (
                    id,
                    run_id,
                    process_id,
                    draft_id,
                    status,
                    pins_before,
                    pins_after,
                    problems,
                    version,
                    author
                )
                values (
                    %(id)s,
                    %(run_id)s,
                    %(process_id)s,
                    %(draft_id)s,
                    %(status)s,
                    %(pins_before)s,
                    %(pins_after)s,
                    %(problems)s,
                    %(version)s,
                    %(author)s
                )
                returning
                    at
                """,
                id=upgrade.id,
                run_id=upgrade.run_id,
                process_id=upgrade.process_id,
                draft_id=upgrade.draft_id,
                status=upgrade.status.value,
                pins_before=Jsonb(self._pins_json(upgrade.pins_before)),
                pins_after=Jsonb(self._pins_json(upgrade.pins_after)),
                problems=Jsonb(problems),
                version=upgrade.version,
                author=Jsonb(upgrade.author.model_dump(mode="json")),
            )
            .build()
        )
        row = self._returning(
            await self._row(insert, f"record upgrade {upgrade.id}"),
            f"insert into process_upgrades for upgrade {upgrade.id}",
        )

        return upgrade.model_copy(update={"at": row["at"]})

    async def last_upgrade_of_process(self, process_id: UUID) -> Upgrade | None:
        query = (
            self._upgrade_query()
            .add(
                """
                where process_id = %(id)s and draft_id is null
                order by at desc limit 1
                """,
                id=process_id,
            )
            .build()
        )

        return await self._last_upgrade(query, process_id)

    async def last_upgrade_of_draft(self, draft_id: UUID) -> Upgrade | None:
        query = (
            self._upgrade_query()
            .add("where draft_id = %(id)s order by at desc limit 1", id=draft_id)
            .build()
        )

        return await self._last_upgrade(query, draft_id)

    def _upgrade_query(self) -> PgQueryBuilder:
        """Строки итогов upgrade; условие вызывающий добавляет следующим куском."""
        return self._query().add(
            """
            select
                id,
                run_id,
                process_id,
                draft_id,
                status,
                pins_before,
                pins_after,
                problems,
                version,
                author,
                at
            from
                {schema}.process_upgrades
            """
        )

    async def _last_upgrade(self, query: PgQuery, target: UUID) -> Upgrade | None:
        row = await self._row(query, f"last upgrade of {target}")
        if row is None:
            return None

        return self._parse(Upgrade, row)

    async def upgrades_of_run(self, run_id: UUID) -> Sequence[Upgrade]:
        query = (
            self._upgrade_query()
            .add("where run_id = %(id)s order by at", id=run_id)
            .build()
        )
        rows = await self._rows(query, f"upgrades of run {run_id}")

        return self._parse_all(Upgrade, rows)

    # --- запуски upgrade ---

    async def start_upgrade_run(self, run: UpgradeRun) -> UpgradeRun:
        """Запись запуска со статусом running."""
        insert = (
            self._query()
            .add(
                """
                insert into {schema}.process_upgrade_runs (
                    id, target, process_id, draft_id,
                    started_by, status, total
                )
                values (
                    %(id)s, %(target)s, %(process_id)s, %(draft_id)s,
                    %(started_by)s, %(status)s, %(total)s
                )
                """,
                id=run.id,
                target=run.target.value,
                process_id=run.process_id,
                draft_id=run.draft_id,
                started_by=run.started_by,
                status=SyncStatus.RUNNING.value,
                total=run.total,
            )
            .build()
        )

        async with self._transaction(f"start upgrade run {run.id}") as cur:
            await cur.execute(insert.text, insert.params)
            return await self._upgrade_run(cur, run.id)

    async def advance_upgrade_run(self, run_id: UUID, upgrade: Upgrade) -> UpgradeRun:
        """Ещё один процесс пройден: счётчики хода."""
        moved = 0
        blocked = 0
        if upgrade.status is UpgradeStatus.MOVED:
            moved = 1
        else:
            blocked = 1

        update = (
            self._query()
            .add(
                """
                update {schema}.process_upgrade_runs
                set done = done + 1,
                    moved = moved + %(moved)s,
                    blocked = blocked + %(blocked)s
                where id = %(id)s
                """,
                id=run_id,
                moved=moved,
                blocked=blocked,
            )
            .build()
        )

        async with self._transaction(f"advance upgrade run {run_id}") as cur:
            await cur.execute(update.text, update.params)
            return await self._upgrade_run(cur, run_id)

    async def close_upgrade_run(
        self, run_id: UUID, status: SyncStatus, error: str | None
    ) -> UpgradeRun:
        """Запуск завершён: итог, время, причина сбоя.

        Ошибки:
        UpgradeClosedError — запуск уже закрыт.
        """
        update = (
            self._query()
            .add(
                """
                update {schema}.process_upgrade_runs
                set status = %(status)s,
                    finished_at = now(),
                    error = %(error)s
                where id = %(id)s
                """,
                id=run_id,
                status=status.value,
                error=error,
            )
            .build()
        )

        async with self._transaction(f"close upgrade run {run_id}") as cur:
            run = await self._upgrade_run(cur, run_id)
            if run.status is not SyncStatus.RUNNING:
                raise UpgradeClosedError(run_id, run.status)

            await cur.execute(update.text, update.params)
            return await self._upgrade_run(cur, run_id)

    async def get_upgrade_run(self, run_id: UUID) -> UpgradeRun:
        async with self._transaction(f"get upgrade run {run_id}") as cur:
            return await self._upgrade_run(cur, run_id)

    async def upgrade_runs(
        self, process_id: UUID | None, draft_id: UUID | None, limit: int
    ) -> Sequence[UpgradeRun]:
        """Последние запуски, касающиеся процесса или черновика: свои и общие
        (по всем процессам); без фильтра — все."""
        query = (
            self._upgrade_run_query()
            .add(
                """
                where (%(process_id)s::uuid is null and %(draft_id)s::uuid is null)
                   or process_id = %(process_id)s
                   or draft_id = %(draft_id)s
                   or target = %(all)s
                order by started_at desc
                limit %(limit)s
                """,
                process_id=process_id,
                draft_id=draft_id,
                all=UpgradeTarget.ALL.value,
                limit=limit,
            )
            .build()
        )
        rows = await self._rows(query, "upgrade runs")

        return self._parse_all(UpgradeRun, rows)

    def _upgrade_run_query(self) -> PgQueryBuilder:
        """Строки запусков upgrade; условие вызывающий добавляет следующим куском."""
        return self._query().add(
            """
            select
                id,
                target,
                process_id,
                draft_id,
                started_by,
                started_at,
                finished_at,
                status,
                total,
                done,
                moved,
                blocked,
                error
            from
                {schema}.process_upgrade_runs
            """
        )

    async def _upgrade_run(self, cur: Cursor, run_id: UUID) -> UpgradeRun:
        query = self._upgrade_run_query().add("where id = %(id)s", id=run_id).build()
        await cur.execute(query.text, query.params)
        row = await cur.fetchone()
        if row is None:
            raise UpgradeNotFoundError(run_id)

        return self._parse(UpgradeRun, row)

    async def set_pins(self, draft_id: UUID, pins: Mapping[UUID, int]) -> Draft:
        """Привязки черновика к версиям снимков: после поднятия до новых."""
        update = (
            self._query()
            .add(
                """
                update {schema}.drafts
                set pins = %(pins)s
                where id = %(draft_id)s
                """,
                draft_id=draft_id,
                pins=Jsonb(self._pins_json(pins)),
            )
            .build()
        )

        async with self._transaction(f"set pins of draft {draft_id}") as cur:
            draft = await self._draft(cur, draft_id, lock=True)
            self._require_open(draft)
            await cur.execute(update.text, update.params)
            return await self._draft(cur, draft_id, lock=False)

    async def rebase(
        self, draft_id: UUID, *, drop_conflicts: bool, resolver: ObjectResolver
    ) -> RebaseResult:
        """Перевод черновика на текущую версию процесса.

        Операции применяются к текущему снимку по одной с проверкой по
        резолверу; не применимые собираются в issues. Без drop_conflicts
        черновик при конфликтах не меняется; с drop_conflicts конфликтные
        операции вычёркиваются из порций, и черновик переводится на текущую
        версию.
        """
        async with self._transaction(f"rebase draft {draft_id}") as cur:
            draft = await self._draft(cur, draft_id, lock=True)
            self._require_open(draft)

            # черновик нового процесса всегда над пустым снимком
            if draft.process_id is None:
                return RebaseResult(draft=draft, issues=())

            current = await self._current_version(cur, draft.process_id)
            if draft.base_version == current:
                return RebaseResult(draft=draft, issues=())

            base = await self._read_snapshot(cur, draft.process_id)
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

                await self._trim_ops(cur, draft_id, portion.seq, trimmed)

            update_base = (
                self._query()
                .add(
                    """
                    update
                        {schema}.drafts
                    set
                        base_version = %(base_version)s
                    where
                        id = %(draft_id)s
                    returning
                        {draft_columns}
                    """,
                    draft_id=draft_id,
                    base_version=current,
                )
                .build()
            )
            await cur.execute(update_base.text, update_base.params)
            row = self._returning(
                await cur.fetchone(),
                f"update of drafts for draft {draft_id} while rebasing to {current}",
            )

        return RebaseResult(draft=self._parse(Draft, row), issues=tuple(issues))

    async def _trim_ops(
        self, cur: Cursor, draft_id: UUID, seq: int, trimmed: OperationList
    ) -> None:
        """Порция без конфликтных операций на место прежней."""
        update = (
            self._query()
            .add(
                """
                update
                    {schema}.draft_ops
                set
                    operations = %(operations)s
                where 1=1
                    and draft_id = %(draft_id)s
                    and seq = %(seq)s
                """,
                draft_id=draft_id,
                seq=seq,
                operations=Jsonb(trimmed.model_dump(mode="json")),
            )
            .build()
        )
        await cur.execute(update.text, update.params)

    # --- ссылки на просмотр ---

    async def create_share(self, process_id: UUID, created_by: UUID) -> Share:
        insert = (
            self._query()
            .add(
                """
                insert into {schema}.shares (token, process_id, created_by)
                values (%(token)s, %(process_id)s, %(created_by)s)
                returning
                    {share_columns}
                """,
                token=secrets.token_urlsafe(self.TOKEN_BYTES),
                process_id=process_id,
                created_by=created_by,
            )
            .build()
        )

        async with self._transaction(f"share process {process_id}") as cur:
            await self._process(cur, process_id)
            await cur.execute(insert.text, insert.params)
            row = self._returning(
                await cur.fetchone(), f"insert into shares for process {process_id}"
            )

        return self._parse(Share, row)

    async def shares_of(self, process_id: UUID) -> Sequence[Share]:
        """Действующие ссылки процесса."""
        query = (
            self._query()
            .add(
                """
                select
                    {share_columns}
                from
                    {schema}.shares
                where 1=1
                    and process_id = %(process_id)s
                    and revoked_at is null
                order by
                    created_at
                """,
                process_id=process_id,
            )
            .build()
        )

        async with self._transaction(f"shares of process {process_id}") as cur:
            await self._process(cur, process_id)
            await cur.execute(query.text, query.params)
            rows = await cur.fetchall()

        return self._parse_all(Share, rows)

    async def get_share(self, token: str) -> Share:
        """Действующая ссылка по token.

        Ошибки:
        ShareNotFoundError — ссылки нет или она отозвана.
        """
        query = (
            self._query()
            .add(
                """
                select
                    {share_columns}
                from
                    {schema}.shares
                where 1=1
                    and token = %(token)s
                    and revoked_at is null
                """,
                token=token,
            )
            .build()
        )
        row = await self._row(query, "get share")
        if row is None:
            raise ShareNotFoundError(token)

        return self._parse(Share, row)

    async def revoke_share(self, token: str) -> Share:
        """Ссылка отозвана: гость больше не пройдёт.

        Ошибки:
        ShareNotFoundError — ссылки нет или она уже отозвана.
        """
        query = (
            self._query()
            .add(
                """
                update
                    {schema}.shares
                set
                    revoked_at = now()
                where 1=1
                    and token = %(token)s
                    and revoked_at is null
                returning
                    {share_columns}
                """,
                token=token,
            )
            .build()
        )
        row = await self._row(query, "revoke share")
        if row is None:
            raise ShareNotFoundError(token)

        return self._parse(Share, row)

    # --- внутреннее: процессы ---

    def _process_query(self) -> PgQueryBuilder:
        """Строки процессов со счётчиками; условие и порядок вызывающий
        добавляет следующим куском с алиасом p."""
        return self._query().add(
            """
            with
                latest as (
                    select
                        v.process_id as process_id,
                        max(v.number) as latest_version
                    from
                        {schema}.process_versions v
                    group by
                        v.process_id
                ),
                node_counts as (
                    select
                        n.process_id as process_id,
                        count(*) as nodes
                    from
                        {schema}.nodes n
                    group by
                        n.process_id
                ),
                draft_counts as (
                    select
                        d.process_id as process_id,
                        count(*) as open_drafts
                    from
                        {schema}.drafts d
                    where
                        d.status = 'open'
                    group by
                        d.process_id
                )
            select
                p.id,
                p.name,
                p.description,
                p.owner_id,
                p.created_at,
                coalesce(l.latest_version, 0) as latest_version,
                coalesce(nc.nodes, 0) as nodes,
                coalesce(dc.open_drafts, 0) as open_drafts,
                coalesce(lv.pins, '{{}}'::jsonb) as pins,
                coalesce(cn.connection_ids, '{{}}'::uuid[]) as connections,
                coalesce(lu.problems, 0) as attention
            from
                {schema}.processes p
                left join latest l on l.process_id = p.id
                left join node_counts nc on nc.process_id = p.id
                left join draft_counts dc on dc.process_id = p.id
                left join lateral (
                    select array_agg(distinct n.connection_id) as connection_ids
                    from {schema}.nodes n
                    where n.process_id = p.id
                ) cn on true
                left join {schema}.process_versions lv
                    on lv.process_id = p.id
                    and lv.number = l.latest_version
                left join lateral (
                    select
                        case
                            when u.status = 'blocked'
                            then jsonb_array_length(u.problems)
                            else 0
                        end as problems
                    from
                        {schema}.process_upgrades u
                    where
                        u.process_id = p.id
                        and u.draft_id is null
                    order by
                        u.at desc
                    limit 1
                ) lu on true
            """
        )

    async def _process(self, cur: Cursor, process_id: UUID) -> Process:
        query = self._process_query().add("where p.id = %(id)s", id=process_id).build()
        await cur.execute(query.text, query.params)
        row = await cur.fetchone()
        if row is None:
            raise ProcessNotFoundError(process_id)

        return self._parse(Process, row)

    # --- внутреннее: снимок ---

    async def _read_snapshot(self, cur: Cursor, process_id: UUID) -> CatalogSnapshot:
        """Снимок из таблиц процесса, проверенный check()."""
        groups = await self._rows_of(
            cur,
            self._query()
            .add(
                """
                select
                    id,
                    name
                from
                    {schema}.groups
                where
                    process_id = %(process_id)s
                order by
                    name,
                    id
                """,
                process_id=process_id,
            )
            .build(),
        )
        nodes = await self._rows_of(
            cur,
            self._query()
            .add(
                """
                select
                    id,
                    group_id,
                    x,
                    y,
                    width,
                    connection_id,
                    object_kind,
                    path,
                    alias,
                    note
                from
                    {schema}.nodes
                where
                    process_id = %(process_id)s
                order by
                    path,
                    id
                """,
                process_id=process_id,
            )
            .build(),
        )
        flows = await self._rows_of(
            cur,
            self._query()
            .add(
                """
                select
                    id,
                    from_node_id,
                    to_node_id,
                    columns,
                    description
                from
                    {schema}.flows
                where
                    process_id = %(process_id)s
                order by
                    id
                """,
                process_id=process_id,
            )
            .build(),
        )

        try:
            return self._assemble(groups, nodes, flows)
        except ValidationError as exc:
            msg = (
                f"catalog: a row of the entity tables of process {process_id} in "
                f"{self.schema} is not a valid entity: {exc}"
            )
            raise CatalogStoreError(msg) from exc
        except CatalogInvariantError as exc:
            msg = (
                f"catalog: entity tables of process {process_id} in "
                f"{self.schema} are inconsistent: {exc}"
            )
            raise CatalogStoreError(msg) from exc

    def _assemble(
        self,
        groups: Sequence[DictRow],
        nodes: Sequence[DictRow],
        flows: Sequence[DictRow],
    ) -> CatalogSnapshot:
        group_table: dict[UUID, Group] = {}
        for row in groups:
            group = Group.model_validate(row)
            group_table[group.id] = group

        node_table: dict[UUID, Node] = {}
        for row in nodes:
            node = self._entities.node_of(row)
            node_table[node.id] = node

        flow_table: dict[UUID, Flow] = {}
        for row in flows:
            flow = self._entities.flow_of(row)
            flow_table[flow.id] = flow

        snapshot = CatalogSnapshot(
            groups=group_table, nodes=node_table, flows=flow_table
        )
        snapshot.check()
        return snapshot

    async def _base_of(self, cur: Cursor, draft: Draft) -> CatalogSnapshot:
        """Базовый снимок черновика: версия процесса либо пустой у черновика
        нового процесса."""
        if draft.process_id is None:
            return CatalogSnapshot.empty()

        return await self._snapshot_at(cur, draft.process_id, draft.base_version)

    async def _attach_process(self, cur: Cursor, draft: Draft) -> UUID:
        """Процесс для черновика нового процесса: имя черновика, владелец —
        автор черновика; черновик привязывается к нему.

        Ошибки:
        ProcessNameTakenError — имя занято.
        """
        process_id = uuid4()
        insert = (
            self._query()
            .add(
                """
                insert into {schema}.processes
                    (id, name, description, owner_id)
                values
                    (%(id)s, %(name)s, '', %(owner_id)s)
                """,
                id=process_id,
                name=draft.name,
                owner_id=draft.created_by,
            )
            .build()
        )
        try:
            await cur.execute(insert.text, insert.params)
        except UniqueViolation as exc:
            raise ProcessNameTakenError(draft.name) from exc

        attach = (
            self._query()
            .add(
                """
                update {schema}.drafts
                set process_id = %(process_id)s
                where id = %(draft_id)s
                """,
                process_id=process_id,
                draft_id=draft.id,
            )
            .build()
        )
        await cur.execute(attach.text, attach.params)

        return process_id

    async def _rows_of(self, cur: Cursor, query: PgQuery) -> Sequence[DictRow]:
        await cur.execute(query.text, query.params)
        return await cur.fetchall()

    async def _current_version(self, cur: Cursor, process_id: UUID) -> int:
        query = (
            self._query()
            .add(
                """
                select coalesce(max(number), 0) as top
                from {schema}.process_versions
                where process_id = %(key)s
                """,
                key=process_id,
            )
            .build()
        )

        return await self._max_of(
            cur, query, f"reading the latest version number of process {process_id}"
        )

    async def _snapshot_at(
        self, cur: Cursor, process_id: UUID, version: int
    ) -> CatalogSnapshot:
        current = await self._current_version(cur, process_id)
        if version == current:
            return await self._read_snapshot(cur, process_id)

        if version > current:
            msg = (
                f"catalog: version {version} of process {process_id} is not "
                f"published yet, the latest is {current}"
            )
            raise CatalogStoreError(msg)

        query = (
            self._query()
            .add(
                """
                select
                    {version_columns}
                from
                    {schema}.process_versions
                where 1=1
                    and process_id = %(process_id)s
                    and number <= %(version)s
                order by
                    number
                """,
                process_id=process_id,
                version=version,
            )
            .build()
        )
        await cur.execute(query.text, query.params)
        rows = await cur.fetchall()

        snapshot = CatalogSnapshot.empty()
        for row in rows:
            stored = self._parse(Version, row)
            try:
                snapshot = stored.operations.apply(snapshot, AcceptAll())
            except CatalogOpError as exc:
                msg = (
                    f"catalog: operations of version {stored.number} of process "
                    f"{process_id} do not apply on top of the previous "
                    f"versions: {exc}"
                )
                raise CatalogStoreError(msg) from exc

        return snapshot

    # --- внутреннее: черновики ---

    def _draft_query(self) -> PgQueryBuilder:
        """Строки черновиков; условие вызывающий добавляет следующим куском."""
        return self._query().add("select {draft_columns} from {schema}.drafts")

    async def _draft(self, cur: Cursor, draft_id: UUID, *, lock: bool) -> Draft:
        query = self._draft_query().add("where id = %(id)s", id=draft_id)
        query.when(lock, "for update")
        built = query.build()

        await cur.execute(built.text, built.params)
        row = await cur.fetchone()
        if row is None:
            raise DraftNotFoundError(draft_id)

        return self._parse(Draft, row)

    def _require_open(self, draft: Draft) -> None:
        if draft.status is DraftStatus.OPEN:
            return

        raise DraftClosedError(draft.id, draft.status)

    async def _set_status(
        self, cur: Cursor, draft_id: UUID, status: DraftStatus
    ) -> Draft:
        query = (
            self._query()
            .add(
                """
                update
                    {schema}.drafts
                set
                    status = %(status)s
                where
                    id = %(id)s
                returning
                    {draft_columns}
                """,
                id=draft_id,
                status=status.value,
            )
            .build()
        )
        await cur.execute(query.text, query.params)
        row = await cur.fetchone()
        if row is None:
            raise DraftNotFoundError(draft_id)

        return self._parse(Draft, row)

    async def _last_seq(self, cur: Cursor, draft_id: UUID) -> int:
        query = (
            self._query()
            .add(
                """
                select coalesce(max(seq), 0) as top
                from {schema}.draft_ops
                where draft_id = %(key)s
                """,
                key=draft_id,
            )
            .build()
        )

        return await self._max_of(
            cur, query, f"reading the last seq of draft {draft_id}"
        )

    async def _ops_of(self, cur: Cursor, draft_id: UUID) -> Sequence[DraftOp]:
        query = (
            self._query()
            .add(
                """
                select
                    draft_id,
                    seq,
                    author,
                    operations,
                    created_at
                from
                    {schema}.draft_ops
                where
                    draft_id = %(draft_id)s
                order by
                    seq
                """,
                draft_id=draft_id,
            )
            .build()
        )
        await cur.execute(query.text, query.params)

        return self._parse_all(DraftOp, await cur.fetchall())

    def _fold(
        self, draft: Draft, base: CatalogSnapshot, ops: Sequence[DraftOp]
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

    def _pins_json(self, pins: Mapping[UUID, int]) -> dict[str, int]:
        rendered: dict[str, int] = {}
        for connection_id, version in pins.items():
            rendered[str(connection_id)] = version

        return rendered

    def _concatenated(self, ops: Sequence[DraftOp]) -> OperationList:
        combined: list[CatalogOp] = []
        for portion in ops:
            combined.extend(portion.operations.root)

        return OperationList(root=tuple(combined))

    async def _write_changes(
        self,
        cur: Cursor,
        process_id: UUID,
        base: CatalogSnapshot,
        target: CatalogSnapshot,
    ) -> None:
        """Таблицы сущностей по diff: upsert добавленных и изменённых, удаление
        пропавших в порядке зависимостей."""
        diff = CatalogDiff.between(base, target)

        for kind in EntityRows.UPSERT_ORDER:
            rows: list[dict[str, Any]] = []
            for entity in self._changed(diff, target, kind):
                rows.append(self._entities.row_of(process_id, entity))

            if not rows:
                continue

            upsert = self._upsert(kind)
            await cur.executemany(upsert.text, rows)

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

            delete = self._delete(kind, removed)
            await cur.execute(delete.text, delete.params)

    def _changed(
        self, diff: CatalogDiff, target: CatalogSnapshot, kind: EntityKind
    ) -> Iterator[CatalogEntity]:
        table = target.table(kind)
        for entry in diff.entries:
            if entry.ref.kind is not kind:
                continue

            if entry.status is ChangeStatus.REMOVED:
                continue

            yield table[entry.ref.id]

    def _entity_table(self, kind: EntityKind) -> sql.Identifier:
        return sql.Identifier(self.schema, CatalogTable.of_entity(kind).value)

    def _upsert(self, kind: EntityKind) -> PgQuery:
        """Вставка строк сущности с заменой по id; значения приходят строками
        executemany."""
        idents: list[sql.Composable] = []
        placeholders: list[sql.Composable] = []
        updates: list[sql.Composable] = []
        for column in self._entities.columns_of(kind):
            ident = sql.Identifier(column.value)
            idents.append(ident)
            placeholders.append(sql.Placeholder(column.value))
            if column.value == EntityColumn.ID.value:
                continue

            updates.append(sql.SQL("{} = excluded.{}").format(ident, ident))

        return (
            PgQueryBuilder(
                table=self._entity_table(kind),
                columns=sql.SQL(", ").join(idents),
                values=sql.SQL(", ").join(placeholders),
                updates=sql.SQL(", ").join(updates),
            )
            .add(
                """
                insert into {table} ({columns})
                values ({values})
                on conflict (id) do update set {updates}
                """
            )
            .build()
        )

    def _delete(self, kind: EntityKind, ids: Sequence[UUID]) -> PgQuery:
        return (
            PgQueryBuilder(table=self._entity_table(kind))
            .add("delete from {table} where id = any(%(ids)s)", ids=list(ids))
            .build()
        )
