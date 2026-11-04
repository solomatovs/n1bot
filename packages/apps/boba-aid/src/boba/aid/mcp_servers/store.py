"""Хранилище MCP-серверов чата: таблица mcp_servers и выдачи в grants.

Таблица — единственный источник серверов. Строка несёт имя, содержимое
data (адрес, вход и сроки; секреты зашифрованы SecretCipher) и флаг
enabled. Кому сервер выдан, решают строки grants с src_kind mcp_servers:
личная выдача (tgt_kind users) значит «свой сервер» — владелец правит и
удаляет его; выдача роли (tgt_kind roles) — «от роли», только чтение.

Ошибки:
McpServerStoreError — база отказала, строка не сохранилась, её jsonb не
    разбирается как StoredServer либо секрет строки не расшифровался.
McpServerNameTaken — уникальный индекс по имени занят другой строкой.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, ClassVar
from uuid import UUID

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from boba.access.grants import GrantResource, GrantSubject
from boba.access_postgres.grants import GrantedIds
from boba.crypto import SecretCipher, SecretCryptoError
from boba.db.postgres import PgQuery, PostgresPool, PostgresTable
from boba.mcp_client.client import HttpEndpoint, McpServerLimits, SseEndpoint

__all__ = [
    "McpServerColumn",
    "McpServerNameTaken",
    "McpServerStore",
    "McpServerStoreError",
    "StoredMcpServer",
    "StoredServer",
    "UserEndpoint",
]

UserEndpoint = Annotated[HttpEndpoint | SseEndpoint, Field(discriminator="transport")]
"""Транспорты серверов из таблицы: HTTP и SSE. stdio сервером пользователя
не бывает — такой процесс запускается на хосте чата, а не у пользователя."""


class McpServerStoreError(Exception):
    """Строка mcp_servers не прочитана или не сохранена; текст — причина."""


class McpServerNameTaken(McpServerStoreError):  # noqa: N818 — имя отказа в API панели
    """Имя сервера занято другой строкой: индекс idx_mcp_servers_name."""


class McpServerColumn(StrEnum):
    """Колонки проекции mcp_servers, по которым разбирается строка."""

    ID = "id"
    NAME = "name"
    DATA = "data"
    ENABLED = "enabled"
    EDITABLE = "editable"


class StoredServer(BaseModel):
    """Содержимое data: адрес сервера, вход и сроки.

    Секции apps у серверов из таблицы нет: порт песочницы приложений задать
    нечем — конфига серверов больше нет.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint: UserEndpoint
    limits: McpServerLimits


class StoredMcpServer(BaseModel):
    """Строка mcp_servers с расшифрованным содержимым.

    editable — сервер выдан этому пользователю лично: он его правит и
    удаляет. Выданный ролью сервер виден только на чтение.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str = Field(min_length=1)
    enabled: bool
    editable: bool
    server: StoredServer


class McpServerStore(PostgresTable):
    """CRUD над mcp_servers вместе с выдачами в grants.

    Наружу отдаёт StoredMcpServer с расшифрованным содержимым; в базе лежит
    шифротекст. Видимость считает общий граф выдачи GrantedIds: сервер виден
    тому, кому выдан лично или через любую его роль. Создаётся провайдером
    процесса на пул и шифр конфига; зовёт его ServerSource.
    """

    RESOURCE: ClassVar[GrantResource] = GrantResource("mcp_servers")
    LABEL: ClassVar[str] = "mcp_servers"
    """Подпись слоя в текстах ошибок."""

    NAME_INDEX: ClassVar[str] = "idx_mcp_servers_name"
    """Имя уникального индекса: по нему отказ базы узнаётся как занятое имя."""

    def __init__(
        self, pool: PostgresPool, db_schema: str, cipher: SecretCipher
    ) -> None:
        super().__init__(pool, db_schema)
        self._cipher = cipher
        self._grants = GrantedIds()

    def _failure(self, action: str, exc: Exception) -> Exception:
        return McpServerStoreError(self._detail(action, exc))

    async def add(self, name: str, server: StoredServer, owner_id: UUID) -> UUID:
        """Новая строка и личная выдача владельцу одной транзакцией.

        Ошибки:
        McpServerNameTaken — имя занято другой строкой.
        """
        payload = self._cipher.encrypt(server)
        insert_row = (
            self._query()
            .add(
                """
                insert into {schema}.mcp_servers (
                    name,
                    data
                )
                values (
                    %(name)s,
                    %(data)s
                )
                returning
                    id
                """,
                name=name,
                data=Jsonb(payload),
            )
            .build()
        )

        async with self._transaction(f"adding the server {name!r}") as cur:
            try:
                await cur.execute(insert_row.text, insert_row.params)
                row = await cur.fetchone()
            except UniqueViolation as exc:
                raise self._name_taken(f"adding the server {name!r}") from exc

            stored = self._returning(row, f"insert of row {name!r} into mcp_servers")
            server_id = UUID(str(stored[McpServerColumn.ID.value]))
            grant = self._grant_query(server_id, owner_id)
            await cur.execute(grant.text, grant.params)

        return server_id

    async def replace(self, server_id: UUID, name: str, server: StoredServer) -> None:
        """Меняет имя и содержимое; enabled и выдачи не трогает.

        Ошибки:
        McpServerNameTaken — имя занято другой строкой.
        """
        payload = self._cipher.encrypt(server)
        query = (
            self._query()
            .add(
                """
                update
                    {schema}.mcp_servers
                set
                    name = %(name)s,
                    data = %(data)s
                where
                    id = %(id)s
                """,
                id=server_id,
                name=name,
                data=Jsonb(payload),
            )
            .build()
        )
        clear_oauth = (
            self._query()
            .add(
                "delete from {schema}.mcp_tokens where server_id = %(id)s",
                id=server_id,
            )
            .build()
        )

        async with self._transaction(f"replacing the row #{server_id}") as cur:
            try:
                await cur.execute(query.text, query.params)
            except UniqueViolation as exc:
                raise self._name_taken(
                    f"renaming the row #{server_id} to {name!r}"
                ) from exc

            if cur.rowcount != 1:
                raise McpServerStoreError(
                    f"Replacing MCP server {name!r}: row {server_id} no longer exists"
                )

            # An edited address must never receive a token for the old address.
            await cur.execute(clear_oauth.text, clear_oauth.params)

    async def set_enabled(self, server_id: UUID, enabled: bool) -> None:
        query = (
            self._query()
            .add(
                """
                update
                    {schema}.mcp_servers
                set
                    enabled = %(enabled)s
                where
                    id = %(id)s
                """,
                id=server_id,
                enabled=enabled,
            )
            .build()
        )
        changed = await self._execute(query, f"setting enabled of the row #{server_id}")
        if changed != 1:
            raise McpServerStoreError(
                f"Setting MCP server {server_id} enabled={enabled}: "
                "row no longer exists"
            )

    async def remove(self, server_id: UUID) -> bool:
        """Удаляет строку вместе с её выдачами одной транзакцией; False —
        строки не было."""
        drop_grants = (
            self._query()
            .add(
                """
                delete from
                    {schema}.grants
                where 1=1
                    and src_kind = %(src_kind)s
                    and src_kind_id = %(id)s
                """,
                id=server_id,
                src_kind=self.RESOURCE.value,
            )
            .build()
        )
        drop_row = (
            self._query()
            .add(
                """
                delete from
                    {schema}.mcp_servers
                where
                    id = %(id)s
                """,
                id=server_id,
            )
            .build()
        )

        async with self._transaction(f"removing the row #{server_id}") as cur:
            await cur.execute(drop_grants.text, drop_grants.params)
            await cur.execute(drop_row.text, drop_row.params)

            return cur.rowcount > 0

    async def visible_to(
        self, user_id: UUID, roles: Iterable[str]
    ) -> Sequence[StoredMcpServer]:
        """Серверы, выданные пользователю лично или через его роли, в порядке
        имён; enabled — как записано, выключенные тоже видны.

        Ошибки:
        McpServerStoreError — строка не разбирается или секрет не расшифровался.
        """
        query = self._visible_query(user_id, roles)
        rows = await self._rows(query, f"servers granted to the user #{user_id}")

        return [self._stored(row) for row in rows]

    async def granted(
        self, name: str, user_id: UUID, roles: Iterable[str]
    ) -> StoredMcpServer | None:
        """Сервер name, если он выдан пользователю лично или через его роли;
        None — не выдан либо такого нет.

        Ошибки:
        McpServerStoreError — строка не разбирается или секрет не расшифровался.
        """
        query = self._granted_one_query(name, user_id, roles)
        row = await self._row(
            query, f"reading the server {name!r} granted to the user #{user_id}"
        )
        if row is None:
            return None

        return self._stored(row)

    async def by_name(self, name: str) -> StoredMcpServer | None:
        """Строка по имени без учёта выдачи: кому она видна, решает вызывающий.

        Ошибки:
        McpServerStoreError — строка не разбирается или секрет не расшифровался.
        """
        query = (
            self._query()
            .add(
                """
                select
                    s.id as id,
                    s.name as name,
                    s.data as data,
                    s.enabled as enabled,
                    false as editable
                from
                    {schema}.mcp_servers s
                where
                    s.name = %(name)s
                """,
                name=name,
            )
            .build()
        )
        row = await self._row(query, f"reading the row named {name!r}")
        if row is None:
            return None

        return self._stored(row)

    def _visible_query(self, user_id: UUID, roles: Iterable[str]) -> PgQuery:
        """Серверы выдачи с признаком личной выдачи этому пользователю."""
        text = (
            GrantedIds.TEXT
            + """
            select
                s.id as id,
                s.name as name,
                s.data as data,
                s.enabled as enabled,
                exists (
                    select
                        1
                    from
                        {schema}.grants own
                    where 1=1
                        and own.src_kind = %(src_kind)s
                        and own.src_kind_id = s.id
                        and own.tgt_kind = %(users_kind)s
                        and own.tgt_kind_id = %(user_id)s
                ) as editable
            from
                {schema}.mcp_servers s
                inner join granted on granted.src_id = s.id
            order by
                s.name
        """
        )
        params = self._grants.params(user_id, roles, self.RESOURCE)

        return self._query().add(text, **params).build()

    def _granted_one_query(
        self, name: str, user_id: UUID, roles: Iterable[str]
    ) -> PgQuery:
        """Один сервер из выдачи, если он выдан; признак личной выдачи не нужен."""
        text = (
            GrantedIds.TEXT
            + """
            select
                s.id as id,
                s.name as name,
                s.data as data,
                s.enabled as enabled,
                false as editable
            from
                {schema}.mcp_servers s
                inner join granted on granted.src_id = s.id
            where
                s.name = %(name)s
        """
        )
        params = self._grants.params(user_id, roles, self.RESOURCE)

        return self._query().add(text, name=name, **params).build()

    def _grant_query(self, server_id: UUID, owner_id: UUID) -> PgQuery:
        """Строка выдачи сервера владельцу; повтор той же связи ничего не меняет."""
        return (
            self._query()
            .add(
                """
                insert into {schema}.grants (
                    src_kind,
                    src_kind_id,
                    tgt_kind,
                    tgt_kind_id
                )
                values (
                    %(src_kind)s,
                    %(src_kind_id)s,
                    %(tgt_kind)s,
                    %(tgt_kind_id)s
                )
                on conflict (src_kind, src_kind_id, tgt_kind, tgt_kind_id)
                    do nothing
                """,
                src_kind=self.RESOURCE.value,
                src_kind_id=server_id,
                tgt_kind=GrantSubject.USERS.value,
                tgt_kind_id=owner_id,
            )
            .build()
        )

    def _name_taken(self, action: str) -> McpServerNameTaken:
        return McpServerNameTaken(
            f"mcp_servers: {action} failed: a server with this name is already "
            f"stored, expected a free name by the index {self.NAME_INDEX}"
        )

    def _stored(self, row: Mapping[str, Any]) -> StoredMcpServer:
        """Строка с расшифрованным содержимым.

        Ошибки:
        McpServerStoreError — секрет не расшифровался или jsonb не по форме.
        """
        server_id = row[McpServerColumn.ID.value]
        name = row[McpServerColumn.NAME.value]
        try:
            content = self._cipher.decrypt(row[McpServerColumn.DATA.value])
        except SecretCryptoError as exc:
            msg = (
                f"mcp_servers: reading the row {name!r} (#{server_id}) failed: "
                f"its secrets did not decrypt with the configured key: {exc}"
            )
            raise McpServerStoreError(msg) from exc

        try:
            server = StoredServer.model_validate(content)
        except ValidationError as exc:
            # from None: во входе pydantic лежат расшифрованные секреты
            fields = ", ".join(self._field_paths(exc))
            msg = (
                f"mcp_servers: the row {name!r} (#{server_id}) is not a server "
                f"description, fields off the form: {fields}"
            )
            raise McpServerStoreError(msg) from None

        return StoredMcpServer(
            id=UUID(str(server_id)),
            name=name,
            enabled=bool(row[McpServerColumn.ENABLED.value]),
            editable=bool(row[McpServerColumn.EDITABLE.value]),
            server=server,
        )

    def _field_paths(self, exc: ValidationError) -> Sequence[str]:
        """Пути полей отказа без значений: во входе лежит расшифрованный секрет."""
        paths: list[str] = []
        for error in exc.errors(include_input=False):
            path = ".".join(str(part) for part in error["loc"])
            paths.append(path or "<root>")

        return paths
