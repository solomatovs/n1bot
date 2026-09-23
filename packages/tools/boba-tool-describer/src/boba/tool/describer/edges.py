"""Рёбра описаний: модели, SQL, таблица и инструменты describe_edge,
describe_list_edges, describe_delete_edge.

Ребро — связь двух узлов одной области; концы называются url, а их id
таблица ищет своим SQL по таблице узлов, не завися от кода узлов. Модуль
самодостаточен: от соседей ему нужны только сессия хранилища и реестр
адресов. Запуск: `python -m boba.tool.describer.edges <имя> --флаги`.

Ошибки:
AddressError — url конца не является адресом ни одного семейства.
EdgeEndMissingError — конец ребра не описан в области; к тексту приложены
    известные url области.
EdgeIdsMissingError — среди id на удаление есть не из области; ничего не
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
from pydantic import BaseModel, ConfigDict, Field

from boba.connections.address import Address, AddressError
from boba.db.postgres import PostgresError
from boba.identity.context import Scope
from boba.tool.describer.address import Addresses
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
    "EdgeDelete",
    "EdgeEndMissingError",
    "EdgeIdsMissingError",
    "EdgeKind",
    "EdgeRecord",
    "EdgeSpec",
    "EdgeTable",
    "EdgeWrite",
    "describe_delete_edge",
    "describe_edge",
    "describe_list_edges",
]


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


class EdgeEndMissingError(Exception):
    """Конец ребра не описан в области."""

    def __init__(self, url: str, known: Sequence[str]) -> None:
        msg = (
            f"node {url!r} is not described in this scope yet, call describe_node "
            f"first; described nodes: {list(known)}"
        )
        super().__init__(msg)
        self.url = url
        self.known = tuple(known)


class EdgeIdsMissingError(Exception):
    """Часть id рёбер на удаление не найдена в области; ничего не удалено."""

    def __init__(self, missing: Sequence[int]) -> None:
        msg = (
            f"edge ids {list(missing)} are not described in this scope, "
            "nothing was deleted; take ids from describe_list_edges"
        )
        super().__init__(msg)
        self.missing = tuple(missing)


class EdgeColumn(StrEnum):
    """Колонки, которые читаются из строк выдачи SQL."""

    ID = "id"
    SOURCE = "source"
    TARGET = "target"
    KIND = "kind"
    DESCRIPTION = "description"
    INSERTED = "inserted"
    URL = "url_address"


class EdgeRecord(BaseModel):
    """Ребро области с url обоих концов."""

    id: int
    source: str
    target: str
    kind: EdgeKind
    description: str


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
    description: str


class EdgeDelete(BaseModel):
    """Итог удаления рёбер: снятые id."""

    ids: tuple[int, ...]


class EdgeEnd(BaseModel):
    """Найденный конец ребра: id для записи и url для сообщений."""

    id: int
    url: str


class EdgeTable:
    """Рёбра одной сессии: запись, список и удаление в области; концы ищутся
    по таблице узлов своим SQL."""

    def __init__(self, session: DescriberSession) -> None:
        self._conn = session.conn
        self._session = session

    async def upsert(self, scope: ScopeKey, spec: EdgeSpec) -> EdgeWrite:
        source = await self._end(scope, spec.source)
        target = await self._end(scope, spec.target)
        query = (
            self._session.query()
            .add(
                """
                insert into {schema}.edge (
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
                """,
                source_id=source.id,
                target_id=target.id,
                kind=spec.kind.value,
                description=spec.description,
            )
            .build()
        )

        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query.text, query.params)
            row = await cur.fetchone()

        if row is None:
            msg = (
                f"describer: upsert of edge {source.url!r} -> {target.url!r} "
                "returned no row"
            )
            raise DescriberError(msg)

        return EdgeWrite(
            source=source.url,
            target=target.url,
            kind=spec.kind,
            description=spec.description,
            action=WriteAction.of(bool(row[EdgeColumn.INSERTED.value])),
        )

    async def list(self, scope: ScopeKey) -> Sequence[EdgeRecord]:
        query = (
            self._session.query()
            .add(
                """
                select
                    e.id,
                    s.url_address as source,
                    t.url_address as target,
                    e.kind,
                    e.description
                from
                    {schema}.edge e
                    inner join {schema}.node s on s.id = e.source_id
                    inner join {schema}.node t on t.id = e.target_id
                where
                    s.scope_id = %(scope_id)s
                order by
                    e.id
                """,
                scope_id=scope.id,
            )
            .build()
        )

        records: list[EdgeRecord] = []
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query.text, query.params)

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

    async def delete(self, scope: ScopeKey, ids: Sequence[int]) -> EdgeDelete:
        """Снять рёбра области одним запросом: область — условие удаления,
        нехватка вернувшихся id — откат транзакции."""
        wanted = list(ids)
        query = (
            self._session.query()
            .add(
                """
                delete from {schema}.edge e
                using {schema}.node s
                where 1=1
                    and e.source_id = s.id
                    and s.scope_id = %(scope_id)s
                    and e.id = any(%(ids)s)
                returning
                    e.id
                """,
                scope_id=scope.id,
                ids=wanted,
            )
            .build()
        )

        async with self._conn.transaction():
            removed: set[int] = set()

            async with self._conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(query.text, query.params)

                for row in await cur.fetchall():
                    removed.add(int(row[EdgeColumn.ID.value]))

            missing = MissingIds(wanted, removed).ids()
            if missing:
                raise EdgeIdsMissingError(missing)

        return EdgeDelete(ids=tuple(wanted))

    async def _end(self, scope: ScopeKey, address: Address) -> EdgeEnd:
        """Конец ребра по адресу; нет — EdgeEndMissingError с известными url."""
        query = (
            self._session.query()
            .add(
                """
                select
                    id,
                    url_address
                from
                    {schema}.node
                where 1=1
                    and scope_id = %(scope_id)s
                    and address = %(address)s
                """,
                scope_id=scope.id,
                address=Jsonb(address.to_json()),
            )
            .build()
        )

        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query.text, query.params)
            row = await cur.fetchone()

        if row is None:
            raise EdgeEndMissingError(address.render(), await self._known_ends(scope))

        return EdgeEnd(id=row[EdgeColumn.ID.value], url=row[EdgeColumn.URL.value])

    async def _known_ends(self, scope: ScopeKey) -> list[str]:
        query = (
            self._session.query()
            .add(
                """
                select
                    url_address
                from
                    {schema}.node
                where
                    scope_id = %(scope_id)s
                order by
                    id
                """,
                scope_id=scope.id,
            )
            .build()
        )

        known: list[str] = []
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query.text, query.params)

            for row in await cur.fetchall():
                known.append(row[EdgeColumn.URL.value])

        return known


class EdgePrompt:
    """Тексты аргументов инструментов ребра для модели."""

    SOURCE: ClassVar[str] = (
        "url узла-источника ровно так, как его вернул describe_node (колонка url)."
    )
    TARGET: ClassVar[str] = "url узла-приёмника из ответа describe_node."
    DESCRIPTION: ClassVar[str] = (
        "Как именно связаны объекты: какие колонки совпадают, чем это доказано "
        "(DDL, запрос к данным), в какую сторону идут данные."
    )
    IDS: ClassVar[str] = (
        "id рёбер на удаление — колонка id в describe_list_edges. "
        "Все id должны быть из этого треда, иначе не удаляется ничего."
    )

    @classmethod
    def kind(cls) -> str:
        return f"Вид связи source → target:\n{EdgeKind.prompt()}"


class EdgeListColumn(StrEnum):
    """Колонки выдачи describe_list_edges."""

    ID = "id"
    SOURCE = "source"
    TARGET = "target"
    KIND = "kind"
    DESCRIPTION = "description"


class EdgeListing:
    """Рёбра области таблицей для модели."""

    EMPTY_NOTE: ClassVar[str] = "no edges are described in this scope yet"

    @classmethod
    def result(cls, edges: Sequence[EdgeRecord]) -> TableResult:
        rows: list[dict[str, Any]] = []
        for edge in edges:
            rows.append(
                {
                    EdgeListColumn.ID.value: edge.id,
                    EdgeListColumn.SOURCE.value: edge.source,
                    EdgeListColumn.TARGET.value: edge.target,
                    EdgeListColumn.KIND.value: edge.kind.value,
                    EdgeListColumn.DESCRIPTION.value: edge.description,
                }
            )

        note: str | None = None
        if not rows:
            note = cls.EMPTY_NOTE

        return TableResult(rows=rows, note=note)


class EdgeDeleteColumn(StrEnum):
    """Колонки выдачи describe_delete_edge: по строке на снятый id."""

    ID = "id"
    ACTION = "action"


class EdgeDeleteListing:
    """Строки ответа удаления рёбер."""

    ACTION: ClassVar[str] = "deleted"

    @classmethod
    def result(cls, deleted: EdgeDelete) -> TableResult:
        rows: list[dict[str, Any]] = []
        for edge_id in deleted.ids:
            rows.append(
                {
                    EdgeDeleteColumn.ID.value: edge_id,
                    EdgeDeleteColumn.ACTION.value: cls.ACTION,
                }
            )

        return TableResult(rows=rows)


@tool
async def describe_edge(  # noqa: PLR0913 — оба конца, вид и текст называет вызов
    source: Annotated[str, Field(min_length=1, description=EdgePrompt.SOURCE)],
    target: Annotated[str, Field(min_length=1, description=EdgePrompt.TARGET)],
    kind: Annotated[EdgeKind, Field(description=EdgePrompt.kind())],
    description: Annotated[
        str, Field(min_length=1, description=EdgePrompt.DESCRIPTION)
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

    async with DescriberStore(cfg).session() as session:
        written = await EdgeTable(session).upsert(key, spec)

    return TableResult(rows=[written.model_dump(mode="json")])


@tool
async def describe_list_edges(
    scope: Annotated[Scope, Injected],
    cfg: Annotated[DescriberToolConfig, Injected],
) -> TableResult:
    """Рёбра, уже описанные в этом треде: id, source, target, kind, description.

    Помогает не описывать связь дважды и брать id для describe_delete_edge.
    """
    key = ScopeKey.of(scope)

    async with DescriberStore(cfg).session() as session:
        edges = await EdgeTable(session).list(key)

    return EdgeListing.result(edges)


@tool
async def describe_delete_edge(
    ids: Annotated[list[int], Field(min_length=1, description=EdgePrompt.IDS)],
    scope: Annotated[Scope, Injected],
    cfg: Annotated[DescriberToolConfig, Injected],
) -> TableResult:
    """Удалить рёбра этого треда по id; узлы остаются.

    Всё или ничего: если хоть один id не из этого треда, не удаляется ничего.
    """
    key = ScopeKey.of(scope)

    async with DescriberStore(cfg).session() as session:
        deleted = await EdgeTable(session).delete(key, ids)

    return EdgeDeleteListing.result(deleted)


EXPECTED: Mapping[type[Exception], DescriberErrorKind] = {
    AddressError: DescriberErrorKind.INVALID_ADDRESS,
    EdgeEndMissingError: DescriberErrorKind.NODE_MISSING,
    EdgeIdsMissingError: DescriberErrorKind.EDGE_ID_MISSING,
    DescriberError: DescriberErrorKind.INVALID_SCOPE,
    PostgresError: DescriberErrorKind.DATABASE_UNAVAILABLE,
    psycopg.Error: DescriberErrorKind.SQL_FAILED,
}

TOOLS: Final = ToolMain.toolset(
    describe_edge, describe_list_edges, describe_delete_edge
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
