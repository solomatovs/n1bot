"""Хранилище описаний: таблицы node и edge в базе приложения.

Узел — адрес объекта с описанием в области вызова (тред, запуск workflow,
задание); ребро — связь двух узлов той же области. Таблицы идемпотентно
готовит сам store на каждой сессии: внешний потребитель забирает строки и
делает truncate, схему и таблицы не трогает. Запись — upsert: повторное
описание того же объекта или той же связи обновляет текст.

Ошибки:
DescriberError — область вызова не годится ключом: id не uuid.
NodeMissingError — конец ребра не описан в области; к тексту приложены
    известные url области.
PostgresError — до базы приложения не достучаться.
psycopg.Error — СУБД отклонила запрос.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from boba.connections.address import Address
from boba.db.postgres import PayloadPostgres
from boba.db.postgres.profile import PostgresConfig
from boba.identity.context import Scope, ScopeKind

logger = logging.getLogger(__name__)

__all__ = [
    "DescriberError",
    "DescriberStore",
    "EdgeKind",
    "EdgeRecord",
    "EdgeSpec",
    "EdgeWrite",
    "NodeMissingError",
    "NodeRecord",
    "NodeWrite",
    "ScopeKey",
    "WriteAction",
]


class DescriberError(Exception):
    """Область вызова не годится ключом хранилища."""


class NodeMissingError(Exception):
    """Конец ребра не описан в области."""

    def __init__(self, url: str, known: Sequence[str]) -> None:
        msg = (
            f"node {url!r} is not described in this scope yet, call describe_node "
            f"first; described nodes: {list(known)}"
        )
        super().__init__(msg)
        self.url = url
        self.known = tuple(known)


class DescriberTable(StrEnum):
    """Таблицы хранилища."""

    NODE = "node"
    EDGE = "edge"


class NodeColumn(StrEnum):
    """Колонки node, которые читаются из строк выдачи."""

    ID = "id"
    KIND = "kind"
    URL = "url_address"
    DESCRIPTION = "description"
    INSERTED = "inserted"


class EdgeColumn(StrEnum):
    """Колонки выдачи по edge."""

    ID = "id"
    SOURCE = "source"
    TARGET = "target"
    KIND = "kind"
    DESCRIPTION = "description"
    INSERTED = "inserted"


class EdgeKind(StrEnum):
    """Вид связи source → target; значения ограничены, чтобы потребитель не
    получал свободный текст."""

    FOREIGN_KEY = "foreign_key"
    IMPLICIT_KEY = "implicit_key"
    DERIVED_FROM = "derived_from"
    SIMILAR = "similar"

    @property
    def meaning(self) -> str:
        if self is EdgeKind.FOREIGN_KEY:
            return "связь объявлена в DDL ограничением FK: source ссылается на target"

        if self is EdgeKind.IMPLICIT_KEY:
            return (
                "ограничения нет, но значения совпадают: join-ключ, найденный "
                "запросами к данным, в том числе между разными системами"
            )

        if self is EdgeKind.DERIVED_FROM:
            return (
                "target построен из source: view, matview, словарь, ETL-копия; "
                "направление данных"
            )

        return "объекты про одно и то же, но ни ключа, ни потока данных не доказано"

    @classmethod
    def prompt(cls) -> str:
        lines: list[str] = []
        for kind in cls:
            lines.append(f"{kind.value} — {kind.meaning}")

        return "\n".join(lines)


class WriteAction(StrEnum):
    """Что сделал upsert."""

    INSERTED = "inserted"
    UPDATED = "updated"

    @classmethod
    def of(cls, inserted: bool) -> WriteAction:
        if inserted:
            return cls.INSERTED

        return cls.UPDATED


class ScopeKey(BaseModel):
    """Область вызова ключом таблиц: вид и uuid."""

    model_config = ConfigDict(frozen=True)

    kind: ScopeKind
    id: UUID

    @classmethod
    def of(cls, scope: Scope) -> ScopeKey:
        try:
            scope_id = UUID(scope.id)
        except ValueError as exc:
            msg = (
                f"describer: scope {scope.kind.value} id {scope.id!r} is not a uuid, "
                "descriptions are keyed by uuid scopes"
            )
            raise DescriberError(msg) from exc

        return cls(kind=scope.kind, id=scope_id)


class NodeRecord(BaseModel):
    """Узел области как он лежит в таблице."""

    id: int
    kind: str
    url: str
    description: str


class EdgeRecord(BaseModel):
    """Ребро области с url обоих концов."""

    id: int
    source: str
    target: str
    kind: EdgeKind
    description: str


class NodeWrite(BaseModel):
    """Итог записи узла: канонический url для дальнейших ссылок."""

    kind: str
    url: str
    action: WriteAction


class EdgeSpec(BaseModel):
    """Что записать ребром: адреса концов, вид и описание."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    source: Address
    target: Address
    kind: EdgeKind
    description: str


class EdgeWrite(BaseModel):
    """Итог записи ребра."""

    source: str
    target: str
    kind: EdgeKind
    action: WriteAction


class NodeRef(BaseModel):
    """Найденный узел области: id для ребра и url для сообщений."""

    id: int
    url: str


class DescriberSql:
    """Тексты SQL хранилища; имена таблиц подставляются идентификаторами."""

    LOCK: ClassVar[sql.SQL] = sql.SQL("select pg_advisory_xact_lock(hashtext(%(key)s))")
    SCHEMA: ClassVar[str] = "create schema if not exists {schema}"
    DDL: ClassVar[str] = """
create table if not exists {node} (
    id          bigserial primary key,
    scope_kind  varchar not null,
    scope_id    uuid not null,
    kind        varchar not null,
    address     jsonb not null,
    url_address varchar not null,
    description varchar not null,
    s__wrt_ts   timestamptz not null default now()
);
create unique index if not exists node_uk   on {node} (scope_id, address);
create index if not exists node_address_gin on {node} using gin (address jsonb_path_ops);
create index if not exists node_kind_btree  on {node} (kind);
create table if not exists {edge} (
    id          bigserial primary key,
    source_id   bigint not null references {node} on delete cascade,
    target_id   bigint not null references {node} on delete cascade,
    kind        varchar not null,
    description varchar not null,
    s__wrt_ts   timestamptz not null default now(),
    unique (source_id, target_id, kind)
)
"""
    UPSERT_NODE: ClassVar[str] = """
insert into {node} (
    scope_kind,
    scope_id,
    kind,
    address,
    url_address,
    description
)
values (
    %(scope_kind)s,
    %(scope_id)s,
    %(kind)s,
    %(address)s,
    %(url)s,
    %(description)s
)
on conflict (scope_id, address) do update set
    kind        = excluded.kind,
    url_address = excluded.url_address,
    description = excluded.description,
    s__wrt_ts   = now()
returning
    (xmax = 0) as inserted
"""
    FIND_NODE: ClassVar[str] = """
select
    id,
    url_address
from {node}
where 1=1
    and scope_id = %(scope_id)s
    and address = %(address)s
"""
    UPSERT_EDGE: ClassVar[str] = """
insert into {edge} (
    source_id,
    target_id,
    kind,
    description
)
values (
    %(source_id)s,
    %(target_id)s,
    %(kind)s,
    %(description)s
)
on conflict (source_id, target_id, kind)
do update set
    description = excluded.description,
    s__wrt_ts   = now()
returning
    (xmax = 0) as inserted
"""
    NODES: ClassVar[str] = """
select
    id,
    kind,
    url_address,
    description
from
    {node}
where
    scope_id = %(scope_id)s
order by
    id
"""
    EDGES: ClassVar[str] = """
select
    e.id,
    s.url_address as source,
    t.url_address as target,
    e.kind,
    e.description
from
    {edge} e
    inner join {node} s on s.id = e.source_id
    inner join {node} t on t.id = e.target_id
where
    s.scope_id = %(scope_id)s
order by
    e.id
"""

class DescriberStore:
    """Сессии над таблицами node/edge: одно соединение на вызов инструмента,
    таблицы готовы к первому запросу."""

    DDL_LOCK: ClassVar[str] = "boba.describer.ddl"

    def __init__(self, connection: PostgresConfig, db_schema: str) -> None:
        self._connection = connection
        self._schema = db_schema

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[psycopg.AsyncConnection[Any], None]:
        """Соединение с готовыми таблицами; закрывается по выходу."""
        conn = await PayloadPostgres.connect_config(self._connection)
        async with conn:
            await self._ensure(conn)
            yield conn

    async def _ensure(self, conn: psycopg.AsyncConnection[Any]) -> None:
        """Схема и таблицы под одним advisory-замком: параллельные вызовы одного
        ответа модели иначе роняют create schema if not exists на уникальности
        pg_namespace. Без права на create schema её заводит администратор."""
        async with conn.transaction():
            await conn.execute(DescriberSql.LOCK, {"key": self.DDL_LOCK})

            try:
                async with conn.transaction():
                    await conn.execute(self._sql(DescriberSql.SCHEMA))
            except InsufficientPrivilege:
                logger.info(
                    "no permission for create schema %r, assuming an administrator "
                    "created it",
                    self._schema,
                )

            await conn.execute(self._sql(DescriberSql.DDL))

    async def upsert_node(
        self,
        conn: psycopg.AsyncConnection[Any],
        scope: ScopeKey,
        address: Address,
        description: str,
    ) -> NodeWrite:
        url = address.render()
        params = {
            "scope_kind": scope.kind.value,
            "scope_id": scope.id,
            "kind": type(address).KIND,
            "address": Jsonb(address.to_json()),
            "url": url,
            "description": description,
        }

        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(self._sql(DescriberSql.UPSERT_NODE), params)
            row = await cur.fetchone()

        if row is None:
            msg = f"describer: upsert of node {url!r} returned no row"
            raise DescriberError(msg)

        return NodeWrite(
            kind=type(address).KIND,
            url=url,
            action=WriteAction.of(bool(row[NodeColumn.INSERTED.value])),
        )

    async def upsert_edge(
        self, conn: psycopg.AsyncConnection[Any], scope: ScopeKey, spec: EdgeSpec
    ) -> EdgeWrite:
        source_ref = await self._find_node(conn, scope, spec.source)
        target_ref = await self._find_node(conn, scope, spec.target)

        params = {
            "source_id": source_ref.id,
            "target_id": target_ref.id,
            "kind": spec.kind.value,
            "description": spec.description,
        }

        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(self._sql(DescriberSql.UPSERT_EDGE), params)
            row = await cur.fetchone()

        if row is None:
            msg = (
                f"describer: upsert of edge {source_ref.url!r} -> {target_ref.url!r} "
                "returned no row"
            )
            raise DescriberError(msg)

        return EdgeWrite(
            source=source_ref.url,
            target=target_ref.url,
            kind=spec.kind,
            action=WriteAction.of(bool(row[EdgeColumn.INSERTED.value])),
        )

    async def nodes(
        self, conn: psycopg.AsyncConnection[Any], scope: ScopeKey
    ) -> Sequence[NodeRecord]:
        records: list[NodeRecord] = []

        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(self._sql(DescriberSql.NODES), {"scope_id": scope.id})

            for row in await cur.fetchall():
                records.append(
                    NodeRecord(
                    id=row[NodeColumn.ID.value],
                    kind=row[NodeColumn.KIND.value],
                    url=row[NodeColumn.URL.value],
                    description=row[NodeColumn.DESCRIPTION.value],
                )
            )

        return records

    async def edges(
        self, conn: psycopg.AsyncConnection[Any], scope: ScopeKey
    ) -> Sequence[EdgeRecord]:
        records: list[EdgeRecord] = []

        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(self._sql(DescriberSql.EDGES), {"scope_id": scope.id})

            for row in await cur.fetchall():
                records.append(
                    EdgeRecord(
                        id=row[EdgeColumn.ID.value],
                        source=row[EdgeColumn.SOURCE.value],
                        target=row[EdgeColumn.TARGET.value],
                        kind=EdgeKind(row[EdgeColumn.KIND.value]),
                        description=row[EdgeColumn.DESCRIPTION.value],
                    )
                )

        return records

    async def _find_node(
        self, conn: psycopg.AsyncConnection[Any], scope: ScopeKey, address: Address
    ) -> NodeRef:
        params = {"scope_id": scope.id, "address": Jsonb(address.to_json())}

        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(self._sql(DescriberSql.FIND_NODE), params)
            row = await cur.fetchone()

        if row is None:
            known: list[str] = []
            for node in await self.nodes(conn, scope):
                known.append(node.url)

            raise NodeMissingError(address.render(), known)

        return NodeRef(id=row[NodeColumn.ID.value], url=row[NodeColumn.URL.value])

    def _sql(self, template: str) -> sql.Composed:
        return sql.SQL(template).format(  # type: ignore[arg-type]
            schema=sql.Identifier(self._schema),
            node=sql.Identifier(self._schema, DescriberTable.NODE.value),
            edge=sql.Identifier(self._schema, DescriberTable.EDGE.value),
        )
