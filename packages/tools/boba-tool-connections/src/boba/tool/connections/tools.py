"""Tool connection_list: соединения, выданные субъекту вызова.

Единственный способ, которым модель узнаёт доступные ей соединения: имя,
вид и описание. Тело читает таблицы connections/roles/grants приложения
своим подключением из [tool.connections] и отбирает строки, выданные
пользователю лично или любой его роли. Секреты профилей не читаются:
в выдаче только открытые поля jsonb.

Ошибки:
PostgresError — до базы приложения не достучаться (сеть, libpq, kerberos).
psycopg.Error — СУБД отклонила запрос к таблицам соединений.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final, LiteralString

from psycopg import sql
from psycopg.rows import dict_row
from pydantic import Field

from boba.connections.profile import (
    ConnectionsColumn,
    ConnectionTable,
    GrantKind,
    GrantsColumn,
    RolesColumn,
)
from boba.db.postgres import PayloadPostgres, SqlNames
from boba.db.postgres.profile import PostgresConfig
from boba.identity.context import Subject
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import TableResult
from boba.toolkit.types import SecretRevealing

__all__ = ["TOOLS", "ConnectionsToolConfig", "connection_list"]


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
    """Колонки выдачи connection_list; их же читает модель в ответе."""

    NAME = "connection"
    KIND = "kind"
    DESCRIPTION = "description"


class ProfileKey(StrEnum):
    """Открытые ключи jsonb профиля, нужные выдаче."""

    KIND = "kind"
    DESCRIPTION = "description"


class GrantedConnections:
    """Выборка строк субъекта: лично выданные и выданные любой его роли.

    Имя, выданное субъекту дважды внутри одного вида, не показывается: вызов
    всё равно отвергнет его как неоднозначное, выбирать модели не из чего.
    """

    EMPTY_NOTE: ClassVar[str] = "no connections are granted to you"

    QUERY: ClassVar[LiteralString] = """
        with
        subject_roles as (
            select
                r.{r_id}
            from
                {roles} r
            where
                r.{r_role} = any(%(roles)s)
        ),
        granted as (
            select
                g.{g_src_kind_id} as connection_id
            from
                {grants} g
            where 1=1
                and g.{g_src_kind} = %(src_kind)s
                and g.{g_tgt_kind} = %(users_kind)s
                and g.{g_tgt_kind_id} = %(user_id)s
            union
            select
                g.{g_src_kind_id} as connection_id
            from
                {grants} g
                inner join subject_roles sr on g.{g_tgt_kind_id} = sr.{r_id}
            where 1=1
                and g.{g_src_kind} = %(src_kind)s
                and g.{g_tgt_kind} = %(roles_kind)s
        ),
        visible as (
            select
                c.{c_name} as name,
                c.{c_data} ->> %(kind_key)s as kind,
                coalesce(c.{c_data} ->> %(description_key)s, '') as description
            from
                {connections} c
                inner join granted on granted.connection_id = c.{c_id}
        )
        select
            name,
            kind,
            min(description) as description
        from
            visible
        group by
            kind,
            name
        having
            count(*) = 1
        order by
            kind,
            name
    """

    def __init__(self, cfg: ConnectionsToolConfig) -> None:
        self._cfg = cfg

    async def rows(self, subject: Subject) -> TableResult:
        conn = await PayloadPostgres.connect_config(self._cfg.connection)
        try:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(self._query(), self._params(subject))
                found = await cur.fetchall()
        finally:
            await conn.close()

        rows: list[dict[str, Any]] = []
        for row in found:
            rows.append(
                {
                    CatalogColumn.NAME.value: row["name"],
                    CatalogColumn.KIND.value: row["kind"],
                    CatalogColumn.DESCRIPTION.value: row["description"],
                }
            )

        return TableResult(rows=rows, note=self._note(len(rows)))

    def _query(self) -> sql.Composed:
        schema = self._cfg.db_schema
        names: dict[str, sql.Composable] = {
            "connections": SqlNames.table(schema, ConnectionTable.CONNECTIONS),
            "roles": SqlNames.table(schema, ConnectionTable.ROLES),
            "grants": SqlNames.table(schema, ConnectionTable.GRANTS),
        }
        for column in ConnectionsColumn:
            names[f"c_{column.value}"] = SqlNames.ident(column)

        for column in RolesColumn:
            names[f"r_{column.name.lower()}"] = SqlNames.ident(column)

        for column in GrantsColumn:
            names[f"g_{column.value}"] = SqlNames.ident(column)

        return sql.SQL(self.QUERY).format(**names)

    @staticmethod
    def _params(subject: Subject) -> dict[str, Any]:
        return {
            "src_kind": GrantKind.CONNECTIONS.value,
            "users_kind": GrantKind.USERS.value,
            "roles_kind": GrantKind.ROLES.value,
            "user_id": subject.user_id,
            "roles": sorted(subject.roles),
            "kind_key": ProfileKey.KIND.value,
            "description_key": ProfileKey.DESCRIPTION.value,
        }

    @classmethod
    def _note(cls, count: int) -> str | None:
        if count:
            return None

        return cls.EMPTY_NOTE


@tool
async def connection_list(
    subject: Annotated[Subject, Injected],
    cfg: Annotated[ConnectionsToolConfig, Injected],
) -> TableResult:
    """Соединения, доступные пользователю: имя, вид (postgres, clickhouse,
    web, ...) и описание. Имя из этого списка передаётся инструментам в
    параметр соединения; вид говорит, какому инструменту имя подходит."""
    return await GrantedConnections(cfg).rows(subject)


TOOLS: Final = ToolMain.toolset(connection_list)

if __name__ == "__main__":
    raise SystemExit(ToolMain.run(TOOLS))
