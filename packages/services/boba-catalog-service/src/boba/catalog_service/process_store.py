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
from typing import Any, ClassVar, LiteralString
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
from boba.catalog_service.store_base import CatalogStoreBase, Cursor
from boba.db.postgres import AsyncPostgresPool, SqlNames

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

    @classmethod
    def columns_of(cls, kind: EntityKind) -> tuple[StrEnum, ...]:
        """Колонки, которые пишет публикация."""
        if kind is EntityKind.GROUP:
            return tuple(GroupsColumn)

        if kind is EntityKind.NODE:
            return tuple(NodesColumn)

        return tuple(FlowsColumn)

    @staticmethod
    def row_of(process_id: UUID, entity: CatalogEntity) -> dict[str, Any]:
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

    @staticmethod
    def node_of(row: DictRow) -> Node:
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

    @staticmethod
    def flow_of(row: DictRow) -> Flow:
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
    знает.
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

    TABLES: ClassVar[type[StrEnum]] = CatalogTable
    PREFIXED: ClassVar[Mapping[str, type[StrEnum]]] = {
        "p": ProcessesColumn,
        "g": GroupsColumn,
        "n": NodesColumn,
        "f": FlowsColumn,
        "v": VersionsColumn,
        "dr": DraftsColumn,
        "op": DraftOpsColumn,
        "sh": SharesColumn,
        "up": UpgradesColumn,
        "ur": UpgradeRunsColumn,
    }
    COLUMN_LISTS: ClassVar[Mapping[str, LiteralString]] = {
        "draft_columns": (
            "{dr_id}, {dr_process_id}, {dr_name}, {dr_base_version}, {dr_status}, "
            "{dr_pins}, {dr_created_by}, {dr_created_at}"
        ),
        "version_columns": (
            "{v_process_id}, {v_number}, {v_operations}, {v_author}, {v_pins}, "
            "{v_published_at}"
        ),
        "share_columns": (
            "{sh_token}, {sh_process_id}, {sh_created_by}, {sh_created_at}, "
            "{sh_revoked_at}"
        ),
    }

    def __init__(
        self, cfg: CatalogConfig, pool: AsyncPostgresPool | None = None
    ) -> None:
        super().__init__(cfg, cfg.app_schema, pool)

    async def setup(self) -> None:
        """Схема приложения и таблицы; повтор безвреден. Таблицы выпуска, где
        процессы жили в схеме домена, переезжают на месте; иная раскладка —
        отказ с расхождением колонок."""
        async with self._guarded("setup"):
            await self._apply_ddl(())
            await self._move_from_domain()
            await self._apply_ddl(self._ddl())
            await self._check_layouts(self._layouts())

        logger.info("catalog processes ready: %s", self._schema)

    async def _move_from_domain(self) -> None:
        """Таблицы процессов, оставшиеся в схеме домена, — в схему приложения;
        пустые дубли в схеме домена сносятся."""
        domain = self._cfg.db_schema
        if domain == self._schema:
            return

        async with self._transaction(
            "move process tables from the domain schema"
        ) as cur:
            for table in CatalogTable:
                await self._move_table(cur, domain, self._schema, table.value)

    def _layouts(self) -> dict[str, list[str]]:
        layouts: dict[str, list[str]] = {}
        for table, columns in self.LAYOUT.items():
            names: list[str] = []
            for column in columns:
                names.append(column.value)

            layouts[table.value] = names

        return layouts

    def _ddl(self) -> tuple[sql.Composed, ...]:
        return (
            self._sql(
                """
                create table if not exists {processes} (
                    {p_id}          uuid primary key,
                    {p_name}        text not null unique,
                    {p_description} text not null default '',
                    {p_owner_id}    uuid not null,
                    {p_created_at}  timestamptz not null default now()
                )
                """
            ),
            self._sql(
                """
                create table if not exists {groups} (
                    {g_id}          uuid primary key,
                    {g_process_id}  uuid not null references {processes} ({p_id})
                                    on delete cascade,
                    {g_name}        text not null,
                    unique ({g_process_id}, {g_name}) deferrable initially deferred
                )
                """
            ),
            self._sql(
                """
                create table if not exists {nodes} (
                    {n_id}            uuid primary key,
                    {n_process_id}    uuid not null references {processes} ({p_id})
                                      on delete cascade,
                    {n_group_id}      uuid null references {groups} ({g_id})
                                      deferrable initially deferred,
                    {n_x}             double precision null,
                    {n_y}             double precision null,
                    {n_width}         double precision null,
                    {n_connection_id} uuid not null,
                    {n_object_kind}   text not null,
                    {n_path}          text[] not null,
                    {n_alias}         text null,
                    {n_note}          text not null default '',
                    unique ({n_process_id}, {n_connection_id}, {n_object_kind},
                            {n_path})
                        deferrable initially deferred
                )
                """
            ),
            self._sql(
                """
                create table if not exists {flows} (
                    {f_id}           uuid primary key,
                    {f_process_id}   uuid not null references {processes} ({p_id})
                                     on delete cascade,
                    {f_from_node_id} uuid not null references {nodes} ({n_id})
                                     deferrable initially deferred,
                    {f_to_node_id}   uuid not null references {nodes} ({n_id})
                                     deferrable initially deferred,
                    {f_columns}      jsonb not null default '[]'::jsonb,
                    {f_description}  text not null default ''
                )
                """
            ),
            self._sql(
                """
                create table if not exists {process_versions} (
                    {v_process_id}   uuid not null references {processes} ({p_id})
                                     on delete cascade,
                    {v_number}       integer not null,
                    {v_operations}   jsonb not null,
                    {v_author}       jsonb not null,
                    {v_pins}         jsonb not null default '{{}}'::jsonb,
                    {v_published_at} timestamptz not null default now(),
                    primary key ({v_process_id}, {v_number})
                )
                """
            ),
            self._sql(
                """
                create table if not exists {drafts} (
                    {dr_id}           uuid primary key,
                    {dr_process_id}   uuid null references {processes} ({p_id})
                                      on delete cascade,
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
                create table if not exists {shares} (
                    {sh_token}      text primary key,
                    {sh_process_id} uuid not null references {processes} ({p_id})
                                    on delete cascade,
                    {sh_created_by} uuid not null,
                    {sh_created_at} timestamptz not null default now(),
                    {sh_revoked_at} timestamptz null
                )
                """
            ),
            self._sql(
                """
                create table if not exists {process_upgrade_runs} (
                    {ur_id}          uuid primary key,
                    {ur_target}      text not null,
                    {ur_process_id}  uuid null references {processes} ({p_id})
                                     on delete cascade,
                    {ur_draft_id}    uuid null references {drafts} ({dr_id})
                                     on delete cascade,
                    {ur_started_by}  uuid not null,
                    {ur_started_at}  timestamptz not null default now(),
                    {ur_finished_at} timestamptz null,
                    {ur_status}      text not null,
                    {ur_total}       integer not null default 0,
                    {ur_done}        integer not null default 0,
                    {ur_moved}       integer not null default 0,
                    {ur_blocked}     integer not null default 0,
                    {ur_error}       text null
                )
                """
            ),
            self._sql(
                """
                create table if not exists {process_upgrades} (
                    {up_id}          uuid primary key,
                    {up_run_id}      uuid not null references {process_upgrade_runs}
                                     ({ur_id}) on delete cascade,
                    {up_process_id}  uuid null references {processes} ({p_id})
                                     on delete cascade,
                    {up_draft_id}    uuid null references {drafts} ({dr_id})
                                     on delete cascade,
                    {up_status}      text not null,
                    {up_pins_before} jsonb not null,
                    {up_pins_after}  jsonb not null,
                    {up_problems}    jsonb not null,
                    {up_version}     integer null,
                    {up_author}      jsonb not null,
                    {up_at}          timestamptz not null default now()
                )
                """
            ),
            self._sql(
                """
                create index if not exists process_upgrades_process_at
                    on {process_upgrades} ({up_process_id}, {up_at} desc)
                """
            ),
            self._sql(
                """
                create index if not exists process_upgrades_draft_at
                    on {process_upgrades} ({up_draft_id}, {up_at} desc)
                """
            ),
            *self._migrations(),
        )

    def _migrations(self) -> tuple[sql.Composed, ...]:
        """Перевод таблиц прежних выпусков на месте: ширина карточки у узла,
        запуск у итога upgrade (итоги без запуска остаются от выпуска, где
        upgrade был синхронным вызовом — им запуск не нужен)."""
        return (
            self._sql(
                """
                alter table {nodes}
                    add column if not exists {n_width} double precision null
                """
            ),
            self._sql(
                """
                alter table {process_upgrades}
                    add column if not exists {up_run_id} uuid null
                        references {process_upgrade_runs} ({ur_id}) on delete cascade
                """
            ),
        )

    # --- процессы ---

    async def create_process(self, spec: ProcessSpec, owner_id: UUID) -> Process:
        """Новый процесс без версий.

        Ошибки:
        ProcessNameTakenError — имя занято.
        """
        process_id = uuid4()
        async with self._transaction(f"create process {spec.name!r}") as cur:
            try:
                await cur.execute(
                    self._sql(
                        """
                        insert into {processes}
                            ({p_id}, {p_name}, {p_description}, {p_owner_id})
                        values
                            (%(id)s, %(name)s, %(description)s, %(owner_id)s)
                        """
                    ),
                    {
                        "id": process_id,
                        "name": spec.name,
                        "description": spec.description,
                        "owner_id": owner_id,
                    },
                )
            except UniqueViolation as exc:
                raise ProcessNameTakenError(spec.name) from exc

            return await self._process(cur, process_id)

    async def get_process(self, process_id: UUID) -> Process:
        async with self._transaction(f"get process {process_id}") as cur:
            return await self._process(cur, process_id)

    async def list_processes(self) -> Sequence[Process]:
        async with self._transaction("list processes") as cur:
            await cur.execute(self._process_select(" order by p.{p_name}, p.{p_id}"))
            rows = await cur.fetchall()

        return self._parse_all(Process, rows)

    async def update_process(self, process_id: UUID, spec: ProcessSpec) -> Process:
        """Имя и описание процесса.

        Ошибки:
        ProcessNameTakenError — имя занято другим процессом.
        """
        async with self._transaction(f"update process {process_id}") as cur:
            await self._process(cur, process_id)
            try:
                await cur.execute(
                    self._sql(
                        """
                        update {processes}
                        set {p_name} = %(name)s, {p_description} = %(description)s
                        where {p_id} = %(id)s
                        """
                    ),
                    {
                        "id": process_id,
                        "name": spec.name,
                        "description": spec.description,
                    },
                )
            except UniqueViolation as exc:
                raise ProcessNameTakenError(spec.name) from exc

            return await self._process(cur, process_id)

    async def delete_process(self, process_id: UUID) -> bool:
        """Процесс со всем содержимым; False — процесса не было."""
        async with self._transaction(f"delete process {process_id}") as cur:
            await cur.execute(
                self._sql("delete from {processes} where {p_id} = %(id)s"),
                {"id": process_id},
            )
            return cur.rowcount > 0

    async def usage_of_connection(self, connection_id: UUID) -> Sequence[NodeUsage]:
        """Опубликованные узлы над объектами подключения по процессам."""
        query = self._sql(
            """
            select
                p.{p_id} as process_id,
                p.{p_name} as process_name,
                count(*) as nodes
            from
                {nodes} n
                join {processes} p on p.{p_id} = n.{n_process_id}
            where
                n.{n_connection_id} = %(connection_id)s
            group by
                p.{p_id}, p.{p_name}
            order by
                p.{p_name}
            """
        )

        async with self._transaction(f"usage of connection {connection_id}") as cur:
            await cur.execute(query, {"connection_id": connection_id})
            rows = await cur.fetchall()

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
        query = self._sql(
            """
            select
                {version_columns}
            from
                {process_versions}
            where
                {v_process_id} = %(process_id)s
            order by
                {v_number}
            """
        )

        async with self._transaction(f"versions of process {process_id}") as cur:
            await self._process(cur, process_id)
            await cur.execute(query, {"process_id": process_id})
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
        query = self._sql(
            """
            insert into {drafts} (
                {dr_id},
                {dr_process_id},
                {dr_name},
                {dr_base_version},
                {dr_status},
                {dr_pins},
                {dr_created_by}
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
            """
        )

        async with self._transaction(f"create draft {name!r}") as cur:
            current = 0
            if process_id is not None:
                await self._process(cur, process_id)
                current = await self._current_version(cur, process_id)

            params = {
                "id": uuid4(),
                "process_id": process_id,
                "name": name,
                "base_version": current,
                "status": DraftStatus.OPEN.value,
                "pins": Jsonb(self._pins_json(pins)),
                "created_by": created_by,
            }
            await cur.execute(query, params)
            row = self._returning(
                await cur.fetchone(), f"insert into drafts for draft {name!r}"
            )

        return self._parse(Draft, row)

    async def get_draft(self, draft_id: UUID) -> Draft:
        async with self._transaction(f"get draft {draft_id}") as cur:
            return await self._draft(cur, draft_id, lock=False)

    DRAFT_SELECT: ClassVar[LiteralString] = "select {draft_columns} from {drafts}"

    async def open_drafts(self) -> Sequence[Draft]:
        """Открытые черновики всех процессов: для проверки, где стоит подключение."""
        return await self._drafts(
            "list open drafts",
            " where {dr_status} = %(status)s",
            {"status": DraftStatus.OPEN.value},
        )

    async def drafts_of_author(self, created_by: UUID) -> Sequence[Draft]:
        """Открытые черновики автора по всем процессам и без процесса: для
        плоского списка панели."""
        return await self._drafts(
            f"list drafts of {created_by}",
            " where {dr_status} = %(status)s and {dr_created_by} = %(created_by)s",
            {"status": DraftStatus.OPEN.value, "created_by": created_by},
        )

    async def _drafts(
        self, action: str, tail: LiteralString, params: Mapping[str, Any]
    ) -> Sequence[Draft]:
        """Черновики по условию tail в порядке создания."""
        order: LiteralString = " order by {dr_created_at}, {dr_id}"
        query = self._sql(self.DRAFT_SELECT + tail + order)
        async with self._transaction(action) as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()

        return self._parse_all(Draft, rows)

    async def rename_draft(self, draft_id: UUID, name: str) -> Draft:
        """Новое имя открытого черновика."""
        query = self._sql(
            """
            update {drafts}
            set {dr_name} = %(name)s
            where {dr_id} = %(draft_id)s
            returning
                {draft_columns}
            """
        )

        async with self._transaction(f"rename draft {draft_id}") as cur:
            draft = await self._draft(cur, draft_id, lock=True)
            self._require_open(draft)
            await cur.execute(query, {"draft_id": draft_id, "name": name})
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

            base = await self._base_of(cur, draft)
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

            await self._advisory_lock(cur, self.PUBLISH_LOCK, process_id)

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
            await self._advisory_lock(cur, self.PUBLISH_LOCK, process_id)
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
        await cur.execute(
            self._sql(
                """
                insert into {process_versions} (
                    {v_process_id}, {v_number}, {v_operations}, {v_author}, {v_pins}
                )
                values (
                    %(process_id)s, %(number)s, %(operations)s, %(author)s, %(pins)s
                )
                returning {version_columns}
                """
            ),
            {
                "process_id": process_id,
                "number": number,
                "operations": Jsonb(operations.model_dump(mode="json")),
                "author": Jsonb(author.model_dump(mode="json")),
                "pins": Jsonb(self._pins_json(pins)),
            },
        )
        row = self._returning(
            await cur.fetchone(), f"insert of version {number} of process {process_id}"
        )

        return self._parse(Version, row)

    async def record_upgrade(self, upgrade: Upgrade) -> Upgrade:
        """Итог upgrade в историю: последняя запись по процессу (черновику)
        показывается в списке и на странице."""
        query = self._sql(
            """
            insert into {process_upgrades} (
                {up_id},
                {up_run_id},
                {up_process_id},
                {up_draft_id},
                {up_status},
                {up_pins_before},
                {up_pins_after},
                {up_problems},
                {up_version},
                {up_author}
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
                {up_at}
            """
        )
        problems: list[dict[str, Any]] = []
        for problem in upgrade.problems:
            problems.append(problem.model_dump(mode="json"))

        params = {
            "id": upgrade.id,
            "run_id": upgrade.run_id,
            "process_id": upgrade.process_id,
            "draft_id": upgrade.draft_id,
            "status": upgrade.status.value,
            "pins_before": Jsonb(self._pins_json(upgrade.pins_before)),
            "pins_after": Jsonb(self._pins_json(upgrade.pins_after)),
            "problems": Jsonb(problems),
            "version": upgrade.version,
            "author": Jsonb(upgrade.author.model_dump(mode="json")),
        }
        async with self._transaction(f"record upgrade {upgrade.id}") as cur:
            await cur.execute(query, params)
            row = self._returning(
                await cur.fetchone(),
                f"insert into process_upgrades for upgrade {upgrade.id}",
            )

        return upgrade.model_copy(update={"at": row["at"]})

    async def last_upgrade_of_process(self, process_id: UUID) -> Upgrade | None:
        tail: LiteralString = """
            where {up_process_id} = %(id)s and {up_draft_id} is null
            order by {up_at} desc limit 1
        """
        return await self._last_upgrade(tail, process_id)

    async def last_upgrade_of_draft(self, draft_id: UUID) -> Upgrade | None:
        tail: LiteralString = """
            where {up_draft_id} = %(id)s order by {up_at} desc limit 1
        """
        return await self._last_upgrade(tail, draft_id)

    UPGRADE_SELECT: ClassVar[LiteralString] = """
        select
            {up_id},
            {up_run_id},
            {up_process_id},
            {up_draft_id},
            {up_status},
            {up_pins_before},
            {up_pins_after},
            {up_problems},
            {up_version},
            {up_author},
            {up_at}
        from
            {process_upgrades}
        """

    async def _last_upgrade(self, tail: LiteralString, target: UUID) -> Upgrade | None:
        query = sql.Composed([self._sql(self.UPGRADE_SELECT), self._sql(tail)])
        async with self._transaction(f"last upgrade of {target}") as cur:
            await cur.execute(query, {"id": target})
            row = await cur.fetchone()

        if row is None:
            return None

        return self._parse(Upgrade, row)

    async def upgrades_of_run(self, run_id: UUID) -> Sequence[Upgrade]:
        tail: LiteralString = " where {up_run_id} = %(id)s order by {up_at}"
        query = sql.Composed([self._sql(self.UPGRADE_SELECT), self._sql(tail)])
        async with self._transaction(f"upgrades of run {run_id}") as cur:
            await cur.execute(query, {"id": run_id})
            rows = await cur.fetchall()

        return self._parse_all(Upgrade, rows)

    # --- запуски upgrade ---

    async def start_upgrade_run(self, run: UpgradeRun) -> UpgradeRun:
        """Запись запуска со статусом running."""
        query = self._sql(
            """
            insert into {process_upgrade_runs} (
                {ur_id}, {ur_target}, {ur_process_id}, {ur_draft_id},
                {ur_started_by}, {ur_status}, {ur_total}
            )
            values (
                %(id)s, %(target)s, %(process_id)s, %(draft_id)s,
                %(started_by)s, %(status)s, %(total)s
            )
            """
        )
        params = {
            "id": run.id,
            "target": run.target.value,
            "process_id": run.process_id,
            "draft_id": run.draft_id,
            "started_by": run.started_by,
            "status": SyncStatus.RUNNING.value,
            "total": run.total,
        }
        async with self._transaction(f"start upgrade run {run.id}") as cur:
            await cur.execute(query, params)
            return await self._upgrade_run(cur, run.id)

    async def advance_upgrade_run(self, run_id: UUID, upgrade: Upgrade) -> UpgradeRun:
        """Ещё один процесс пройден: счётчики хода."""
        moved = 0
        blocked = 0
        if upgrade.status is UpgradeStatus.MOVED:
            moved = 1
        else:
            blocked = 1

        async with self._transaction(f"advance upgrade run {run_id}") as cur:
            await cur.execute(
                self._sql(
                    """
                    update {process_upgrade_runs}
                    set {ur_done} = {ur_done} + 1,
                        {ur_moved} = {ur_moved} + %(moved)s,
                        {ur_blocked} = {ur_blocked} + %(blocked)s
                    where {ur_id} = %(id)s
                    """
                ),
                {"id": run_id, "moved": moved, "blocked": blocked},
            )
            return await self._upgrade_run(cur, run_id)

    async def close_upgrade_run(
        self, run_id: UUID, status: SyncStatus, error: str | None
    ) -> UpgradeRun:
        """Запуск завершён: итог, время, причина сбоя.

        Ошибки:
        UpgradeClosedError — запуск уже закрыт.
        """
        async with self._transaction(f"close upgrade run {run_id}") as cur:
            run = await self._upgrade_run(cur, run_id)
            if run.status is not SyncStatus.RUNNING:
                raise UpgradeClosedError(run_id, run.status)

            await cur.execute(
                self._sql(
                    """
                    update {process_upgrade_runs}
                    set {ur_status} = %(status)s,
                        {ur_finished_at} = now(),
                        {ur_error} = %(error)s
                    where {ur_id} = %(id)s
                    """
                ),
                {"id": run_id, "status": status.value, "error": error},
            )
            return await self._upgrade_run(cur, run_id)

    async def get_upgrade_run(self, run_id: UUID) -> UpgradeRun:
        async with self._transaction(f"get upgrade run {run_id}") as cur:
            return await self._upgrade_run(cur, run_id)

    async def upgrade_runs(
        self, process_id: UUID | None, draft_id: UUID | None, limit: int
    ) -> Sequence[UpgradeRun]:
        """Последние запуски, касающиеся процесса или черновика: свои и общие
        (по всем процессам); без фильтра — все."""
        query = sql.Composed(
            [
                self._sql(self.UPGRADE_RUN_SELECT),
                self._sql(
                    """
                    where (%(process_id)s::uuid is null and %(draft_id)s::uuid is null)
                       or {ur_process_id} = %(process_id)s
                       or {ur_draft_id} = %(draft_id)s
                       or {ur_target} = %(all)s
                    order by {ur_started_at} desc
                    limit %(limit)s
                    """
                ),
            ]
        )
        params = {
            "process_id": process_id,
            "draft_id": draft_id,
            "all": UpgradeTarget.ALL.value,
            "limit": limit,
        }
        async with self._transaction("upgrade runs") as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()

        return self._parse_all(UpgradeRun, rows)

    UPGRADE_RUN_SELECT: ClassVar[LiteralString] = """
        select
            {ur_id},
            {ur_target},
            {ur_process_id},
            {ur_draft_id},
            {ur_started_by},
            {ur_started_at},
            {ur_finished_at},
            {ur_status},
            {ur_total},
            {ur_done},
            {ur_moved},
            {ur_blocked},
            {ur_error}
        from
            {process_upgrade_runs}
        """

    async def _upgrade_run(self, cur: Cursor, run_id: UUID) -> UpgradeRun:
        query = sql.Composed(
            [self._sql(self.UPGRADE_RUN_SELECT), self._sql(" where {ur_id} = %(id)s")]
        )
        await cur.execute(query, {"id": run_id})
        row = await cur.fetchone()
        if row is None:
            raise UpgradeNotFoundError(run_id)

        return self._parse(UpgradeRun, row)

    async def set_pins(self, draft_id: UUID, pins: Mapping[UUID, int]) -> Draft:
        """Привязки черновика к версиям снимков: после поднятия до новых."""
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
        """Перевод черновика на текущую версию процесса.

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
                {draft_columns}
            """
        )

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

                params = {
                    "draft_id": draft_id,
                    "seq": portion.seq,
                    "operations": Jsonb(trimmed.model_dump(mode="json")),
                }
                await cur.execute(update_ops, params)

            await cur.execute(
                update_base, {"draft_id": draft_id, "base_version": current}
            )
            row = self._returning(
                await cur.fetchone(),
                f"update of drafts for draft {draft_id} while rebasing to {current}",
            )

        return RebaseResult(draft=self._parse(Draft, row), issues=tuple(issues))

    # --- ссылки на просмотр ---

    async def create_share(self, process_id: UUID, created_by: UUID) -> Share:
        query = self._sql(
            """
            insert into {shares} ({sh_token}, {sh_process_id}, {sh_created_by})
            values (%(token)s, %(process_id)s, %(created_by)s)
            returning
                {share_columns}
            """
        )
        params = {
            "token": secrets.token_urlsafe(self.TOKEN_BYTES),
            "process_id": process_id,
            "created_by": created_by,
        }

        async with self._transaction(f"share process {process_id}") as cur:
            await self._process(cur, process_id)
            await cur.execute(query, params)
            row = self._returning(
                await cur.fetchone(), f"insert into shares for process {process_id}"
            )

        return self._parse(Share, row)

    async def shares_of(self, process_id: UUID) -> Sequence[Share]:
        """Действующие ссылки процесса."""
        query = self._sql(
            """
            select
                {share_columns}
            from
                {shares}
            where 1=1
                and {sh_process_id} = %(process_id)s
                and {sh_revoked_at} is null
            order by
                {sh_created_at}
            """
        )

        async with self._transaction(f"shares of process {process_id}") as cur:
            await self._process(cur, process_id)
            await cur.execute(query, {"process_id": process_id})
            rows = await cur.fetchall()

        return self._parse_all(Share, rows)

    async def get_share(self, token: str) -> Share:
        """Действующая ссылка по token.

        Ошибки:
        ShareNotFoundError — ссылки нет или она отозвана.
        """
        query = self._sql(
            """
            select
                {share_columns}
            from
                {shares}
            where 1=1
                and {sh_token} = %(token)s
                and {sh_revoked_at} is null
            """
        )

        async with self._transaction("get share") as cur:
            await cur.execute(query, {"token": token})
            row = await cur.fetchone()

        if row is None:
            raise ShareNotFoundError(token)

        return self._parse(Share, row)

    async def revoke_share(self, token: str) -> Share:
        """Ссылка отозвана: гость больше не пройдёт.

        Ошибки:
        ShareNotFoundError — ссылки нет или она уже отозвана.
        """
        query = self._sql(
            """
            update
                {shares}
            set
                {sh_revoked_at} = now()
            where 1=1
                and {sh_token} = %(token)s
                and {sh_revoked_at} is null
            returning
                {share_columns}
            """
        )

        async with self._transaction("revoke share") as cur:
            await cur.execute(query, {"token": token})
            row = await cur.fetchone()

        if row is None:
            raise ShareNotFoundError(token)

        return self._parse(Share, row)

    # --- внутреннее: процессы ---

    PROCESS_SELECT: ClassVar[LiteralString] = """
        with
            latest as (
                select
                    v.{v_process_id} as process_id,
                    max(v.{v_number}) as latest_version
                from
                    {process_versions} v
                group by
                    v.{v_process_id}
            ),
            node_counts as (
                select
                    n.{n_process_id} as process_id,
                    count(*) as nodes
                from
                    {nodes} n
                group by
                    n.{n_process_id}
            ),
            draft_counts as (
                select
                    d.{dr_process_id} as process_id,
                    count(*) as open_drafts
                from
                    {drafts} d
                where
                    d.{dr_status} = 'open'
                group by
                    d.{dr_process_id}
            )
        select
            p.{p_id},
            p.{p_name},
            p.{p_description},
            p.{p_owner_id},
            p.{p_created_at},
            coalesce(l.latest_version, 0) as latest_version,
            coalesce(nc.nodes, 0) as nodes,
            coalesce(dc.open_drafts, 0) as open_drafts,
            coalesce(lv.{v_pins}, '{{}}'::jsonb) as pins,
            coalesce(cn.connection_ids, '{{}}'::uuid[]) as connections,
            coalesce(lu.problems, 0) as attention
        from
            {processes} p
            left join latest l on l.process_id = p.{p_id}
            left join node_counts nc on nc.process_id = p.{p_id}
            left join draft_counts dc on dc.process_id = p.{p_id}
            left join lateral (
                select array_agg(distinct n.{n_connection_id}) as connection_ids
                from {nodes} n
                where n.{n_process_id} = p.{p_id}
            ) cn on true
            left join {process_versions} lv
                on lv.{v_process_id} = p.{p_id}
                and lv.{v_number} = l.latest_version
            left join lateral (
                select
                    case
                        when u.{up_status} = 'blocked'
                        then jsonb_array_length(u.{up_problems})
                        else 0
                    end as problems
                from
                    {process_upgrades} u
                where
                    u.{up_process_id} = p.{p_id}
                    and u.{up_draft_id} is null
                order by
                    u.{up_at} desc
                limit 1
            ) lu on true
        """

    def _process_select(self, tail: LiteralString) -> sql.Composed:
        return sql.Composed([self._sql(self.PROCESS_SELECT), self._sql(tail)])

    async def _process(self, cur: Cursor, process_id: UUID) -> Process:
        await cur.execute(
            self._process_select(" where p.{p_id} = %(id)s"), {"id": process_id}
        )
        row = await cur.fetchone()
        if row is None:
            raise ProcessNotFoundError(process_id)

        return self._parse(Process, row)

    # --- внутреннее: снимок ---

    async def _read_snapshot(self, cur: Cursor, process_id: UUID) -> CatalogSnapshot:
        """Снимок из таблиц процесса, проверенный check()."""
        groups = await self._rows(
            cur,
            """
            select
                {g_id},
                {g_name}
            from
                {groups}
            where
                {g_process_id} = %(process_id)s
            order by
                {g_name},
                {g_id}
            """,
            process_id,
        )
        nodes = await self._rows(
            cur,
            """
            select
                {n_id},
                {n_group_id},
                {n_x},
                {n_y},
                {n_width},
                {n_connection_id},
                {n_object_kind},
                {n_path},
                {n_alias},
                {n_note}
            from
                {nodes}
            where
                {n_process_id} = %(process_id)s
            order by
                {n_path},
                {n_id}
            """,
            process_id,
        )
        flows = await self._rows(
            cur,
            """
            select
                {f_id},
                {f_from_node_id},
                {f_to_node_id},
                {f_columns},
                {f_description}
            from
                {flows}
            where
                {f_process_id} = %(process_id)s
            order by
                {f_id}
            """,
            process_id,
        )

        try:
            return self._assemble(groups, nodes, flows)
        except ValidationError as exc:
            msg = (
                f"catalog: a row of the entity tables of process {process_id} in "
                f"{self._schema} is not a valid entity: {exc}"
            )
            raise CatalogStoreError(msg) from exc
        except CatalogInvariantError as exc:
            msg = (
                f"catalog: entity tables of process {process_id} in "
                f"{self._schema} are inconsistent: {exc}"
            )
            raise CatalogStoreError(msg) from exc

    @staticmethod
    def _assemble(
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
            node = EntityRows.node_of(row)
            node_table[node.id] = node

        flow_table: dict[UUID, Flow] = {}
        for row in flows:
            flow = EntityRows.flow_of(row)
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
        try:
            await cur.execute(
                self._sql(
                    """
                    insert into {processes}
                        ({p_id}, {p_name}, {p_description}, {p_owner_id})
                    values
                        (%(id)s, %(name)s, '', %(owner_id)s)
                    """
                ),
                {"id": process_id, "name": draft.name, "owner_id": draft.created_by},
            )
        except UniqueViolation as exc:
            raise ProcessNameTakenError(draft.name) from exc

        await cur.execute(
            self._sql(
                """
                update {drafts}
                set {dr_process_id} = %(process_id)s
                where {dr_id} = %(draft_id)s
                """
            ),
            {"process_id": process_id, "draft_id": draft.id},
        )

        return process_id

    async def _rows(
        self, cur: Cursor, text: LiteralString, process_id: UUID
    ) -> Sequence[DictRow]:
        await cur.execute(self._sql(text), {"process_id": process_id})
        return await cur.fetchall()

    async def _current_version(self, cur: Cursor, process_id: UUID) -> int:
        return await self._max_of(
            cur,
            CatalogTable.VERSIONS,
            VersionsColumn.NUMBER,
            VersionsColumn.PROCESS_ID,
            process_id,
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

        query = self._sql(
            """
            select
                {version_columns}
            from
                {process_versions}
            where 1=1
                and {v_process_id} = %(process_id)s
                and {v_number} <= %(version)s
            order by
                {v_number}
            """
        )
        await cur.execute(query, {"process_id": process_id, "version": version})
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

    async def _draft(self, cur: Cursor, draft_id: UUID, *, lock: bool) -> Draft:
        tail: LiteralString = " where {dr_id} = %(id)s"
        if lock:
            tail = " where {dr_id} = %(id)s for update"

        await cur.execute(self._sql(self.DRAFT_SELECT + tail), {"id": draft_id})
        row = await cur.fetchone()
        if row is None:
            raise DraftNotFoundError(draft_id)

        return self._parse(Draft, row)

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
                {draft_columns}
            """
        )
        await cur.execute(query, {"id": draft_id, "status": status.value})
        row = await cur.fetchone()
        if row is None:
            raise DraftNotFoundError(draft_id)

        return self._parse(Draft, row)

    async def _last_seq(self, cur: Cursor, draft_id: UUID) -> int:
        return await self._max_of(
            cur,
            CatalogTable.DRAFT_OPS,
            DraftOpsColumn.SEQ,
            DraftOpsColumn.DRAFT_ID,
            draft_id,
        )

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

        return self._parse_all(DraftOp, await cur.fetchall())

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
        for connection_id, version in pins.items():
            rendered[str(connection_id)] = version

        return rendered

    @staticmethod
    def _concatenated(ops: Sequence[DraftOp]) -> OperationList:
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
                rows.append(EntityRows.row_of(process_id, entity))

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
            if column.value == EntityColumn.ID.value:
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
            key=sql.Identifier(EntityColumn.ID.value),
            updates=sql.SQL(", ").join(updates),
        )

    def _delete(self, kind: EntityKind) -> sql.Composed:
        return sql.SQL(
            """
            delete from {table} where {key} = any(%(ids)s)
            """
        ).format(
            table=self._table(CatalogTable.of_entity(kind)),
            key=sql.Identifier(EntityColumn.ID.value),
        )
