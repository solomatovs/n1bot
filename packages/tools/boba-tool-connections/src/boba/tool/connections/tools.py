"""Tools connection_list и connection_search: соединения, выданные субъекту.

Так модель узнаёт доступные ей соединения: имя, вид, хост и описание.
connection_list отдаёт все, connection_search — отобранные фильтрами по
колонкам. Тело исполняет доменный SubjectGrantsQuery своим подключением к
базе приложения из [tool.connections]: тот же граф грантов и то же правило
дублей, что у хоста при выборе строки под вызов. Секреты профилей не
читаются: в выдаче только открытые поля jsonb.

Ошибки:
PostgresError — до базы приложения не достучаться (сеть, libpq, kerberos).
psycopg.Error — СУБД отклонила запрос к таблицам соединений.
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final

from psycopg import sql
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field

from boba.access.grants import (
    ConnectionFilter,
    ConnectionNames,
    SubjectGrantsQuery,
    SubjectRowColumn,
)
from boba.db.postgres import PayloadPostgres, SqlNames
from boba.db.postgres.profile import PostgresConfig
from boba.identity.context import Subject
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import TableResult
from boba.toolkit.types import SecretRevealing

__all__ = [
    "TOOLS",
    "ConnectionsToolConfig",
    "connection_list",
    "connection_search",
]


class ConnectionsToolConfig(SecretRevealing):
    """Секция [tool.connections]: база приложения и схема таблиц соединений."""

    SECTION: ClassVar[str] = "tool.connections"

    connection: PostgresConfig = Field(
        description="Подключение к базе приложения, где лежат таблицы соединений.",
    )
    db_schema: str = Field(
        min_length=1,
        description="Схема таблиц connections/roles/grants.",
    )


class CatalogColumn(StrEnum):
    """Колонки выдачи обоих инструментов; их же читает модель в ответе."""

    NAME = "connection"
    KIND = "kind"
    HOST = "host"
    DESCRIPTION = "description"


class OpenProfile(BaseModel):
    """Открытые поля jsonb профиля, нужные выдаче; остальное не читается."""

    model_config = ConfigDict(extra="ignore")

    host: str = ""
    description: str = ""


class GrantedConnections:
    """Каталог соединений субъекта для модели: строки SubjectGrantsQuery в
    раскладке CatalogColumn. Имя-дубль внутри вида не показывается: вызов
    отвергнет его как неоднозначное, выбирать модели не из чего."""

    EMPTY_NOTE: ClassVar[str] = "no connections are granted to you"
    NO_MATCH_NOTE: ClassVar[str] = "no granted connections match the filters"

    def __init__(self, cfg: ConnectionsToolConfig) -> None:
        self._cfg = cfg

    async def rows(self, subject: Subject) -> TableResult:
        """Все соединения субъекта."""
        return await self.search(subject, ConnectionFilter.none())

    async def search(self, subject: Subject, flt: ConnectionFilter) -> TableResult:
        """Соединения субъекта, прошедшие фильтры."""
        unique = flt.model_copy(update={"unique_only": True})

        conn = await PayloadPostgres.connect_config(self._cfg.connection)
        try:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    self._query(unique), SubjectGrantsQuery.params(subject, unique)
                )
                found = await cur.fetchall()
        finally:
            await conn.close()

        rows = list(self._rows(found))

        return TableResult(rows=rows, note=self._note(len(rows), flt))

    @staticmethod
    def _rows(found: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        for row in found:
            open_fields = OpenProfile.model_validate(row[SubjectRowColumn.DATA])

            yield {
                CatalogColumn.NAME.value: row[SubjectRowColumn.NAME],
                CatalogColumn.KIND.value: row[SubjectRowColumn.KIND],
                CatalogColumn.HOST.value: open_fields.host,
                CatalogColumn.DESCRIPTION.value: open_fields.description,
            }

    def _query(self, flt: ConnectionFilter) -> sql.Composed:
        names = SqlNames.mapping(
            self._cfg.db_schema, ConnectionNames.tables(), ConnectionNames.columns()
        )

        return sql.SQL(SubjectGrantsQuery.text(flt)).format(**names)

    @classmethod
    def _note(cls, count: int, flt: ConnectionFilter) -> str | None:
        if count:
            return None

        if flt.empty:
            return cls.EMPTY_NOTE

        return cls.NO_MATCH_NOTE


@tool
async def connection_list(
    subject: Annotated[Subject, Injected],
    cfg: Annotated[ConnectionsToolConfig, Injected],
) -> TableResult:
    """Все соединения, доступные пользователю: имя, вид (postgres, clickhouse,
    web, ...), хост и описание. Имя из этого списка передаётся инструментам в
    параметр соединения; вид говорит, какому инструменту имя подходит."""
    return await GrantedConnections(cfg).rows(subject)


@tool
async def connection_search(  # noqa: PLR0913 — фильтр на каждую колонку выдачи
    kind: Annotated[
        str,
        Field(
            description=(
                "Вид соединения, точное совпадение: postgres, clickhouse, web. "
                "Пусто — любой вид."
            ),
        ),
    ] = "",
    name: Annotated[
        str,
        Field(
            description=(
                "Подстрока имени соединения без учёта регистра. Пусто — любое имя."
            ),
        ),
    ] = "",
    host: Annotated[
        str,
        Field(
            description=(
                "Подстрока хоста сервера без учёта регистра. Пусто — любой хост."
            ),
        ),
    ] = "",
    description: Annotated[
        str,
        Field(
            description=(
                "Слова через пробел; каждое должно встретиться в описании "
                "соединения без учёта регистра. Пусто — любое описание."
            ),
        ),
    ] = "",
    *,
    subject: Annotated[Subject, Injected],
    cfg: Annotated[ConnectionsToolConfig, Injected],
) -> TableResult:
    """Найти соединения, доступные пользователю, фильтрами по колонкам:
    фильтры складываются по И, пустой фильтр не применяется. Возвращает
    connection, kind, host, description; имя из выдачи передаётся
    инструментам в параметр соединения, вид говорит, какому инструменту
    имя подходит."""
    flt = ConnectionFilter(kind=kind, name=name, host=host, description=description)

    return await GrantedConnections(cfg).search(subject, flt)


TOOLS: Final = ToolMain.toolset(connection_list, connection_search)

if __name__ == "__main__":
    raise SystemExit(ToolMain.run(TOOLS))
