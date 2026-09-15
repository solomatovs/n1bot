"""Узлы описаний: модели, SQL, таблица и инструменты describe_node,
describe_list_nodes, describe_delete_node.

Узел — адрес объекта данных с описанием в области вызова. Модуль
самодостаточен: от соседей ему нужны только сессия хранилища и реестр
адресов. Запуск: `python -m boba.tool.describer.nodes <имя> --флаги`.

Ошибки:
AddressError — url не является адресом заявленного вида.
NodeIdsMissingError — среди id на удаление есть не из области; ничего не
    удалено, к тексту приложены отсутствующие id.
DescriberError — область вызова не годится ключом (id не uuid).
PostgresError — до базы приложения не достучаться.
psycopg.Error — СУБД отклонила запрос.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from boba.connections.address import Address, AddressError
from boba.db.postgres import PostgresError
from boba.identity.context import Scope
from boba.tool.describer.address import Addresses, NodeKind
from boba.tool.describer.store import (
    DescriberError,
    DescriberErrorKind,
    DescriberSession,
    DescriberStore,
    DescriberToolConfig,
    MissingIds,
    ScopeKey,
    WriteAction,
)
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import TableResult

__all__ = [
    "TOOLS",
    "NodeDelete",
    "NodeIdsMissingError",
    "NodeRecord",
    "NodeTable",
    "NodeWrite",
    "describe_delete_node",
    "describe_list_nodes",
    "describe_node",
]


class NodeIdsMissingError(Exception):
    """Часть id узлов на удаление не найдена в области; ничего не удалено."""

    def __init__(self, missing: Sequence[int]) -> None:
        msg = (
            f"node ids {list(missing)} are not described in this scope, "
            "nothing was deleted; take ids from describe_list_nodes"
        )
        super().__init__(msg)
        self.missing = tuple(missing)


class NodeColumn(StrEnum):
    """Колонки node, которые читаются из строк выдачи SQL."""

    ID = "id"
    KIND = "kind"
    URL = "url_address"
    DESCRIPTION = "description"
    INSERTED = "inserted"
    COUNT = "count"


class NodeRecord(BaseModel):
    """Узел области как он лежит в таблице."""

    id: int
    kind: str
    url: str
    description: str


class NodeWrite(BaseModel):
    """Итог записи узла: канонический url для дальнейших ссылок."""

    kind: str
    url: str
    action: WriteAction
    description: str


class NodeDelete(BaseModel):
    """Итог удаления узлов: снятые id и рёбра, ушедшие каскадом."""

    ids: tuple[int, ...]
    cascaded_edges: int


class NodeSql:
    """Тексты SQL таблицы node."""

    UPSERT: ClassVar[str] = """
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
    LIST: ClassVar[str] = """
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
    SCOPE_IDS: ClassVar[str] = """
select
    id
from
    {node}
where
    scope_id = %(scope_id)s
    and id = any(%(ids)s)
"""
    CASCADED_EDGES: ClassVar[str] = """
select
    count(*) as count
from
    {edge}
where
    source_id = any(%(ids)s)
    or target_id = any(%(ids)s)
"""
    DELETE: ClassVar[str] = """
delete from {node}
where
    scope_id = %(scope_id)s
    and id = any(%(ids)s)
"""


class NodeTable:
    """Узлы одной сессии: запись, список и удаление в области."""

    def __init__(self, session: DescriberSession) -> None:
        self._conn = session.conn
        self._names = session.names

    async def upsert(
        self, scope: ScopeKey, address: Address, description: str
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

        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(self._names.render(NodeSql.UPSERT), params)
            row = await cur.fetchone()

        if row is None:
            msg = f"describer: upsert of node {url!r} returned no row"
            raise DescriberError(msg)

        return NodeWrite(
            kind=type(address).KIND,
            url=url,
            action=WriteAction.of(bool(row[NodeColumn.INSERTED.value])),
            description=description,
        )

    async def list(self, scope: ScopeKey) -> Sequence[NodeRecord]:
        records: list[NodeRecord] = []

        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(self._names.render(NodeSql.LIST), {"scope_id": scope.id})

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

    async def delete(self, scope: ScopeKey, ids: Sequence[int]) -> NodeDelete:
        """Снять узлы области и каскадом их рёбра; чужой или неизвестный id —
        отказ до удаления."""
        wanted = list(ids)
        params = {"scope_id": scope.id, "ids": wanted}

        async with self._conn.transaction():
            found = await self._scope_ids(params)

            missing = MissingIds.of(wanted, found)
            if missing:
                raise NodeIdsMissingError(missing)

            cascaded = await self._cascaded_edges(params)

            await self._conn.execute(self._names.render(NodeSql.DELETE), params)

        return NodeDelete(ids=tuple(wanted), cascaded_edges=cascaded)

    async def _scope_ids(self, params: dict[str, Any]) -> set[int]:
        found: set[int] = set()

        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(self._names.render(NodeSql.SCOPE_IDS), params)

            for row in await cur.fetchall():
                found.add(int(row[NodeColumn.ID.value]))

        return found

    async def _cascaded_edges(self, params: dict[str, Any]) -> int:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(self._names.render(NodeSql.CASCADED_EDGES), params)
            row = await cur.fetchone()

        if row is None:
            return 0

        return int(row[NodeColumn.COUNT.value])


class NodePrompt:
    """Тексты аргументов инструментов узла для модели."""

    KIND: ClassVar[str] = (
        "Вид объекта. Определяет, какие роли ждёт адрес: pg_* — объекты "
        "PostgreSQL, ch_* — ClickHouse, confluence_* — Confluence, entity — "
        "понятие без системы (сущность предметной области)."
    )
    DESCRIPTION: ClassVar[str] = (
        "Описание объекта словами: что хранит или означает, зачем нужен, "
        "ключевые поля и особенности данных."
    )
    IDS: ClassVar[str] = (
        "id узлов на удаление — колонка id в describe_list_nodes. "
        "Все id должны быть из этого треда, иначе не удаляется ничего."
    )

    @classmethod
    def address(cls) -> str:
        return (
            "Адрес объекта строкой url. PostgreSQL: "
            "postgresql://host:port/database?<роли>; ClickHouse: "
            "clickhouse://host:port/database?<роли>; Confluence: url "
            "REST-объекта; понятие: entity://<имя>. Базовый url соединения "
            "дают pg_address, ch_address, web_address, confluence_address. "
            "Роли по видам:\n"
            f"{Addresses.prompt()}"
        )


class NodeListColumn(StrEnum):
    """Колонки выдачи describe_list_nodes."""

    ID = "id"
    KIND = "kind"
    URL = "url"
    DESCRIPTION = "description"


class NodeListing:
    """Узлы области таблицей для модели."""

    EMPTY_NOTE: ClassVar[str] = "no nodes are described in this scope yet"

    @classmethod
    def result(cls, nodes: Sequence[NodeRecord]) -> TableResult:
        rows: list[dict[str, Any]] = []
        for node in nodes:
            rows.append(
                {
                    NodeListColumn.ID.value: node.id,
                    NodeListColumn.KIND.value: node.kind,
                    NodeListColumn.URL.value: node.url,
                    NodeListColumn.DESCRIPTION.value: node.description,
                }
            )

        note: str | None = None
        if not rows:
            note = cls.EMPTY_NOTE

        return TableResult(rows=rows, note=note)


class NodeDeleteColumn(StrEnum):
    """Колонки выдачи describe_delete_node: по строке на снятый id."""

    ID = "id"
    ACTION = "action"


class NodeDeleteListing:
    """Строки ответа удаления узлов и подпись про рёбра, снятые каскадом."""

    ACTION: ClassVar[str] = "deleted"

    @classmethod
    def result(cls, deleted: NodeDelete) -> TableResult:
        rows: list[dict[str, Any]] = []
        for node_id in deleted.ids:
            rows.append(
                {
                    NodeDeleteColumn.ID.value: node_id,
                    NodeDeleteColumn.ACTION.value: cls.ACTION,
                }
            )

        note: str | None = None
        if deleted.cascaded_edges:
            note = f"{deleted.cascaded_edges} edge(s) of these nodes were deleted too"

        return TableResult(rows=rows, note=note)


@tool
async def describe_node(
    kind: Annotated[NodeKind, Field(description=NodePrompt.KIND)],
    address: Annotated[str, Field(min_length=1, description=NodePrompt.address())],
    description: Annotated[
        str, Field(min_length=1, description=NodePrompt.DESCRIPTION)
    ],
    scope: Annotated[Scope, Injected],
    cfg: Annotated[DescriberToolConfig, Injected],
) -> TableResult:
    """Сохранить описание объекта данных по его адресу.

    Повторное описание того же адреса обновляет текст. В ответе — вид,
    канонический url узла (его использует describe_edge) и action:
    inserted или updated.
    """
    key = ScopeKey.of(scope)
    parsed = Addresses.parse(kind, address)

    async with DescriberStore(cfg).session() as session:
        written = await NodeTable(session).upsert(key, parsed, description)

    return TableResult(rows=[written.model_dump(mode="json")])


@tool
async def describe_list_nodes(
    scope: Annotated[Scope, Injected],
    cfg: Annotated[DescriberToolConfig, Injected],
) -> TableResult:
    """Узлы, уже описанные в этом треде: id, kind, url, description.

    Помогает не описывать объект дважды, брать url для describe_edge и id
    для describe_delete_node.
    """
    key = ScopeKey.of(scope)

    async with DescriberStore(cfg).session() as session:
        nodes = await NodeTable(session).list(key)

    return NodeListing.result(nodes)


@tool
async def describe_delete_node(
    ids: Annotated[list[int], Field(min_length=1, description=NodePrompt.IDS)],
    scope: Annotated[Scope, Injected],
    cfg: Annotated[DescriberToolConfig, Injected],
) -> TableResult:
    """Удалить узлы этого треда по id вместе с их рёбрами.

    Всё или ничего: если хоть один id не из этого треда, не удаляется
    ничего. В ответе — снятые id и число рёбер, ушедших каскадом.
    """
    key = ScopeKey.of(scope)

    async with DescriberStore(cfg).session() as session:
        deleted = await NodeTable(session).delete(key, ids)

    return NodeDeleteListing.result(deleted)


EXPECTED: Mapping[type[Exception], DescriberErrorKind] = {
    AddressError: DescriberErrorKind.INVALID_ADDRESS,
    NodeIdsMissingError: DescriberErrorKind.NODE_ID_MISSING,
    DescriberError: DescriberErrorKind.INVALID_SCOPE,
    PostgresError: DescriberErrorKind.DATABASE_UNAVAILABLE,
    psycopg.Error: DescriberErrorKind.SQL_FAILED,
}

TOOLS: Final = ToolMain.toolset(
    describe_node, describe_list_nodes, describe_delete_node
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
