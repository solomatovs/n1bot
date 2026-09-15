"""Инструменты describer: функции уровня модуля, модуль — обычная программа.

Модель описывает объекты данных по адресу и связи между ними; тело пишет
узлы и рёбра в таблицы базы приложения из [tool.describer]. Адрес — строка
url, которую печатает модель; в описываемые системы тело не ходит, а
проверяет только форму url под заявленный вид. Вспомогательные
describe_*_address отдают базовый url соединения, к которому модель
дописывает роли объекта.

Запуск: `python -m boba.tool.describer.tools <имя> --флаги`.

Ошибки:
AddressError — url не является адресом заявленного вида.
NodeMissingError — конец ребра ещё не описан в области.
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
from pydantic import Field

from boba.connections.address import AddressError
from boba.db.clickhouse.address import ChAddresses
from boba.db.clickhouse.profile import ClickHouseConfig
from boba.db.postgres import PostgresError
from boba.db.postgres.address import PgAddresses
from boba.db.postgres.profile import PostgresConfig
from boba.identity.context import Scope
from boba.tool.describer.address import Addresses, NodeKind
from boba.tool.describer.store import (
    DescriberError,
    DescriberStore,
    EdgeKind,
    EdgeRecord,
    EdgeSpec,
    NodeMissingError,
    NodeRecord,
    ScopeKey,
)
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, UserConnection, tool
from boba.toolkit.result import TableResult
from boba.toolkit.types import SecretRevealing

__all__ = [
    "TOOLS",
    "DescriberToolConfig",
    "describe_ch_address",
    "describe_edge",
    "describe_list",
    "describe_node",
    "describe_pg_address",
]


class DescriberToolConfig(SecretRevealing):
    """Секция [tool.describer]: база приложения и схема таблиц node/edge."""

    SECTION: ClassVar[str] = "tool.describer"

    connection: PostgresConfig = Field(
        description="Подключение к базе приложения, где лежат таблицы описаний.",
    )
    db_schema: str = Field(min_length=1, description="Схема таблиц node и edge.")


class DescriberErrorKind(StrEnum):
    """Ожидаемые отказы инструментов describer."""

    INVALID_ADDRESS = "invalid_address"
    NODE_MISSING = "node_missing"
    INVALID_SCOPE = "invalid_scope"
    DATABASE_UNAVAILABLE = "database_unavailable"
    SQL_FAILED = "sql_failed"


class DescriberPrompt:
    """Тексты аргументов для модели; формы адресов берутся из реестров."""

    NODE_KIND: ClassVar[str] = (
        "Вид объекта. Определяет, какие роли ждёт адрес: pg_* — объекты "
        "PostgreSQL, ch_* — ClickHouse, confluence_* — Confluence, entity — "
        "понятие без системы (сущность предметной области)."
    )
    NODE_DESCRIPTION: ClassVar[str] = (
        "Описание объекта словами: что хранит или означает, зачем нужен, "
        "ключевые поля и особенности данных."
    )
    EDGE_SOURCE: ClassVar[str] = (
        "url узла-источника ровно так, как его вернул describe_node (колонка url)."
    )
    EDGE_TARGET: ClassVar[str] = "url узла-приёмника из ответа describe_node."
    EDGE_DESCRIPTION: ClassVar[str] = (
        "Как именно связаны объекты: какие колонки совпадают, чем это доказано "
        "(DDL, запрос к данным), в какую сторону идут данные."
    )

    @classmethod
    def address(cls) -> str:
        return (
            "Адрес объекта строкой url. PostgreSQL: "
            "postgresql://host:port/database?<роли>; ClickHouse: "
            "clickhouse://host:port/database?<роли>; Confluence: url "
            "REST-объекта; понятие: entity://<имя>. Базовый url соединения "
            "даёт describe_pg_address / describe_ch_address. Роли по видам:\n"
            f"{Addresses.prompt()}"
        )

    @classmethod
    def edge_kind(cls) -> str:
        return f"Вид связи source → target:\n{EdgeKind.prompt()}"


class ListItem(StrEnum):
    """Что за строка в выдаче describe_list."""

    NODE = "node"
    EDGE = "edge"


class ListColumn(StrEnum):
    """Колонки выдачи describe_list."""

    ITEM = "item"
    KIND = "kind"
    URL = "url"
    SOURCE = "source"
    TARGET = "target"
    DESCRIPTION = "description"


class ScopeListing:
    """Узлы и рёбра области одной таблицей с общими колонками."""

    EMPTY_NOTE: ClassVar[str] = "nothing is described in this scope yet"

    @classmethod
    def rows(
        cls, nodes: Sequence[NodeRecord], edges: Sequence[EdgeRecord]
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for node in nodes:
            rows.append(cls._node_row(node))

        for edge in edges:
            rows.append(cls._edge_row(edge))

        return rows

    @staticmethod
    def _node_row(node: NodeRecord) -> dict[str, Any]:
        return {
            ListColumn.ITEM.value: ListItem.NODE.value,
            ListColumn.KIND.value: node.kind,
            ListColumn.URL.value: node.url,
            ListColumn.SOURCE.value: "",
            ListColumn.TARGET.value: "",
            ListColumn.DESCRIPTION.value: node.description,
        }

    @staticmethod
    def _edge_row(edge: EdgeRecord) -> dict[str, Any]:
        return {
            ListColumn.ITEM.value: ListItem.EDGE.value,
            ListColumn.KIND.value: edge.kind.value,
            ListColumn.URL.value: "",
            ListColumn.SOURCE.value: edge.source,
            ListColumn.TARGET.value: edge.target,
            ListColumn.DESCRIPTION.value: edge.description,
        }


class AddressColumn(StrEnum):
    """Колонки выдачи describe_*_address."""

    CONNECTION = "connection"
    URL = "url"


def _store(cfg: DescriberToolConfig) -> DescriberStore:
    return DescriberStore(cfg.connection, cfg.db_schema)


@tool
async def describe_node(
    kind: Annotated[NodeKind, Field(description=DescriberPrompt.NODE_KIND)],
    address: Annotated[str, Field(min_length=1, description=DescriberPrompt.address())],
    description: Annotated[
        str, Field(min_length=1, description=DescriberPrompt.NODE_DESCRIPTION)
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

    store = _store(cfg)
    async with store.session() as conn:
        written = await store.upsert_node(conn, key, parsed, description)

    return TableResult(rows=[written.model_dump(mode="json")])


@tool
async def describe_edge(  # noqa: PLR0913 — оба конца, вид и текст называет вызов
    source: Annotated[
        str, Field(min_length=1, description=DescriberPrompt.EDGE_SOURCE)
    ],
    target: Annotated[
        str, Field(min_length=1, description=DescriberPrompt.EDGE_TARGET)
    ],
    kind: Annotated[EdgeKind, Field(description=DescriberPrompt.edge_kind())],
    description: Annotated[
        str, Field(min_length=1, description=DescriberPrompt.EDGE_DESCRIPTION)
    ],
    scope: Annotated[Scope, Injected],
    cfg: Annotated[DescriberToolConfig, Injected],
) -> TableResult:
    """Сохранить связь между двумя уже описанными узлами.

    Узлы могут быть в разных системах (PostgreSQL ↔ ClickHouse, таблица ↔
    страница Confluence, объект ↔ понятие). Оба конца должны быть описаны
    через describe_node в этом же треде. Повтор той же пары с тем же видом
    обновляет описание.
    """
    key = ScopeKey.of(scope)
    spec = EdgeSpec(
        source=Addresses.parse_any(source),
        target=Addresses.parse_any(target),
        kind=kind,
        description=description,
    )

    store = _store(cfg)
    async with store.session() as conn:
        written = await store.upsert_edge(conn, key, spec)

    return TableResult(rows=[written.model_dump(mode="json")])


@tool
async def describe_list(
    scope: Annotated[Scope, Injected],
    cfg: Annotated[DescriberToolConfig, Injected],
) -> TableResult:
    """Узлы и рёбра, уже описанные в этом треде.

    Колонки: item (node или edge), kind, url узла, source и target ребра,
    description. Помогает не описывать объект дважды и брать url для рёбер.
    """
    key = ScopeKey.of(scope)

    store = _store(cfg)
    async with store.session() as conn:
        nodes = await store.nodes(conn, key)
        edges = await store.edges(conn, key)

    rows = ScopeListing.rows(nodes, edges)

    note: str | None = None
    if not rows:
        note = ScopeListing.EMPTY_NOTE

    return TableResult(rows=rows, note=note)


@tool
async def describe_pg_address(
    connection: Annotated[PostgresConfig, UserConnection],
) -> TableResult:
    """Базовый url соединения PostgreSQL: postgresql://host:port/database.

    Ничего не сохраняет. К url дописываются роли объекта для describe_node:
    ?schema=dm&table=fact_orders.
    """
    base = PgAddresses.base_of(connection)

    row = {
        AddressColumn.CONNECTION.value: connection.source.name,
        AddressColumn.URL.value: base.render(),
    }

    return TableResult(rows=[row])


@tool
async def describe_ch_address(
    connection: Annotated[ClickHouseConfig, UserConnection],
) -> TableResult:
    """Базовый url соединения ClickHouse: clickhouse://host:port/database.

    Ничего не сохраняет. В url — база по умолчанию соединения; объект в
    другой базе адресуется заменой сегмента пути. К url дописываются роли
    объекта для describe_node: ?table=events&column=user_id.
    """
    base = ChAddresses.base_of(connection)

    row = {
        AddressColumn.CONNECTION.value: connection.source.name,
        AddressColumn.URL.value: base.render(),
    }

    return TableResult(rows=[row])


EXPECTED: Mapping[type[Exception], DescriberErrorKind] = {
    AddressError: DescriberErrorKind.INVALID_ADDRESS,
    NodeMissingError: DescriberErrorKind.NODE_MISSING,
    DescriberError: DescriberErrorKind.INVALID_SCOPE,
    PostgresError: DescriberErrorKind.DATABASE_UNAVAILABLE,
    psycopg.Error: DescriberErrorKind.SQL_FAILED,
}

TOOLS: Final = ToolMain.toolset(
    describe_node,
    describe_edge,
    describe_list,
    describe_pg_address,
    describe_ch_address,
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
