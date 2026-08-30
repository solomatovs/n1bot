"""Таблицы connections/roles/grants: профили соединений и кому они выданы.

connections — чистое хранилище профилей, без владельца и без уникальности
имени; связь с пользователями и ролями живёт только в grants.

Ошибки:
ConnectionStoreError — база отказала, строка не сохранилась или её jsonb
    не разбирается как профиль.
ConnectionNotFoundError — в connections нет строки с таким id.
SecretCryptoError — секрет строки не расшифровался ключом конфига.
"""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import asynccontextmanager
from typing import Any, ClassVar, LiteralString
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from boba.connections.postgres import PostgresConfig
from boba.connections.profile import (
    ConnectionKind,
    ConnectionNotFoundError,
    ConnectionProfile,
    ConnectionRepository,
    ConnectionsColumn,
    ConnectionStoreError,
    ConnectionTable,
    GrantKind,
    GrantsColumn,
    GrantTarget,
    RolesColumn,
    StoredConnection,
    StoredRole,
)
from boba.connections.secrets import SecretCipher
from boba.db.postgres import AsyncPostgresPool, PostgresError, PostgresTable, SqlNames
from boba.identity.context import Subject
from boba.toolkit.failure import ValidationText

logger = logging.getLogger(__name__)

__all__ = [
    "ConnectionStore",
    "ConnectionsConfig",
]


class ConnectionsConfig(BaseModel):
    """Секция [connections]: где лежат таблицы и чем шифруются значения."""

    model_config = ConfigDict(extra="ignore")

    KEY_BYTES: ClassVar[int] = 32

    enable: bool = Field(
        default=False,
        description="Создавать таблицы connections/roles/grants при старте.",
    )
    connection: PostgresConfig | None = Field(
        default=None,
        description='Postgres-профиль ссылкой: connection = "${postgres}".',
    )
    db_schema: str = Field(
        min_length=1,
        description="Схема postgres, в которой живут таблицы.",
    )
    encryption_key: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "Ключ шифрования значений: 32 байта в base64. Сгенерировать — "
            'python -c "import base64,secrets;'
            'print(base64.b64encode(secrets.token_bytes(32)).decode())"'
        ),
    )

    @field_validator("encryption_key")
    @classmethod
    def _validate_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw:
            return value

        try:
            decoded = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as e:
            msg = "connections.encryption_key: base64 expected"
            raise ValueError(msg) from e

        if len(decoded) != cls.KEY_BYTES:
            msg = (
                f"connections.encryption_key: {cls.KEY_BYTES}-byte key required, "
                f"got {len(decoded)}"
            )
            raise ValueError(msg)

        return value

    def key_bytes(self) -> bytes:
        raw = self.encryption_key.get_secret_value()
        if not raw:
            msg = "connections.encryption_key is not set"
            raise ValueError(msg)

        return base64.b64decode(raw, validate=True)

    def require_conn(self) -> PostgresConfig:
        if self.connection is None:
            msg = 'connections.connection is not set: connection = "${postgres}"'
            raise ValueError(msg)

        return self.connection


class ConnectionStore(PostgresTable, ConnectionRepository):
    """CRUD над connections/roles/grants: наружу — модели, в базе — шифротекст."""

    _PROFILE: ClassVar[TypeAdapter[ConnectionProfile]] = TypeAdapter(ConnectionProfile)

    def __init__(
        self,
        cfg: ConnectionsConfig,
        pool: AsyncPostgresPool | None = None,
    ) -> None:
        postgres = cfg.connection
        if pool is None:
            postgres = cfg.require_conn()

        super().__init__(postgres, cfg.db_schema, pool)
        self._cfg = cfg
        self._cipher = SecretCipher(cfg.key_bytes())

    def _connections(self) -> sql.Identifier:
        return self._table(ConnectionTable.CONNECTIONS)

    def _roles(self) -> sql.Identifier:
        return self._table(ConnectionTable.ROLES)

    def _grants(self) -> sql.Identifier:
        return self._table(ConnectionTable.GRANTS)

    def _sql(self, text: LiteralString) -> sql.Composed:
        """SQL с именами таблиц и колонок из enum'ов: c_* — connections, r_* — roles,
        g_* — grants.
        """
        names: dict[str, sql.Composable] = {
            "connections": self._connections(),
            "roles": self._roles(),
            "grants": self._grants(),
        }
        for column in ConnectionsColumn:
            names[f"c_{column.value}"] = SqlNames.ident(column)
        for column in RolesColumn:
            names[f"r_{column.name.lower()}"] = SqlNames.ident(column)
        for column in GrantsColumn:
            names[f"g_{column.value}"] = SqlNames.ident(column)

        return sql.SQL(text).format(**names)

    @asynccontextmanager
    async def _guarded(self, action: str) -> AsyncGenerator[None]:
        """Граница слоя: отказ базы или пула уходит наружу как ConnectionStoreError."""
        try:
            yield
        except (psycopg.Error, PostgresError) as exc:
            msg = f"connections: {action} failed"
            raise ConnectionStoreError(msg) from exc

    async def setup(self) -> None:
        """Схема и три таблицы; повтор безвреден."""
        ddl = (*self._connections_ddl(), *self._roles_ddl(), *self._grants_ddl())
        async with self._guarded("setup"):
            await self._apply_ddl(ddl)

        logger.info("connections ready: %s", self._cfg.db_schema)

    def _connections_ddl(self) -> tuple[sql.Composed, ...]:
        return (
            sql.SQL(
                """
                create table if not exists {connections} (
                    {id}   uuid primary key default gen_random_uuid(),
                    {name} text not null,
                    {data} jsonb not null default '{{}}'::jsonb
                )
                """
            ).format(
                connections=self._connections(), **self._columns(ConnectionsColumn)
            ),
            sql.SQL(
                """
                create index if not exists idx_connections_kind
                    on {connections} (({data} ->> 'kind'))
                """
            ).format(
                connections=self._connections(), **self._columns(ConnectionsColumn)
            ),
        )

    def _roles_ddl(self) -> tuple[sql.Composed, ...]:
        return (
            sql.SQL(
                """
                create table if not exists {roles} (
                    {id}        uuid primary key default gen_random_uuid(),
                    {role}      varchar not null unique,
                    {create_at} timestamptz not null default now()
                )
                """
            ).format(roles=self._roles(), **self._columns(RolesColumn)),
        )

    def _grants_ddl(self) -> tuple[sql.Composed, ...]:
        return (
            sql.SQL(
                """
                create table if not exists {grants} (
                    {id}          uuid primary key default gen_random_uuid(),
                    {src_kind}    varchar not null,
                    {src_kind_id} uuid not null,
                    {tgt_kind}    varchar not null,
                    {tgt_kind_id} uuid not null,
                    unique ({src_kind}, {src_kind_id}, {tgt_kind}, {tgt_kind_id})
                )
                """
            ).format(grants=self._grants(), **self._columns(GrantsColumn)),
            sql.SQL(
                """
                create index if not exists idx_grants_target
                    on {grants} ({tgt_kind}, {tgt_kind_id})
                """
            ).format(grants=self._grants(), **self._columns(GrantsColumn)),
        )

    async def sync_roles(self, names: Iterable[str]) -> None:
        """Добавляет в roles имена, которых там ещё нет; ничего не удаляет."""
        query = self._sql(
            """
            insert into {roles} (
                {r_role}
            )
            values (
                %(role)s
            )
            on conflict ({r_role}) do nothing
            """
        )

        rows: list[dict[str, str]] = []
        for name in names:
            rows.append({"role": name})

        if not rows:
            return

        pool = await self._pool()
        async with self._guarded("sync roles"), pool.cursor() as cur:
            await cur.executemany(query, rows)

    async def add(self, name: str, profile: ConnectionProfile) -> UUID:
        """Новая строка connections; уникальность имени — забота вызывающего."""
        payload = self._cipher.encrypt(profile)
        query = self._sql(
            """
            insert into {connections} (
                {c_name},
                {c_data}
            )
            values (
                %(name)s,
                %(data)s
            )
            returning
                {c_id}
            """
        )
        params = {"name": name, "data": Jsonb(payload)}

        pool = await self._pool()
        async with self._guarded("add"), pool.cursor() as cur:
            await cur.execute(query, params)
            row = await cur.fetchone()

        if row is None:
            msg = f"connections: row {name!r} was not saved"
            raise ConnectionStoreError(msg)

        return UUID(str(row[0]))

    async def add_owned(
        self, name: str, profile: ConnectionProfile, user_id: UUID
    ) -> UUID:
        """Строка и личный грант одной транзакцией: личный грант и есть владение."""
        payload = self._cipher.encrypt(profile)
        insert_row = self._sql(
            """
            insert into {connections} (
                {c_name},
                {c_data}
            )
            values (
                %(name)s,
                %(data)s
            )
            returning
                {c_id}
            """
        )
        insert_grant = self._sql(
            """
            insert into {grants} (
                {g_src_kind},
                {g_src_kind_id},
                {g_tgt_kind},
                {g_tgt_kind_id}
            )
            values (
                %(src_kind)s,
                %(src_kind_id)s,
                %(tgt_kind)s,
                %(tgt_kind_id)s
            )
            """
        )

        pool = await self._pool()
        async with self._guarded("add_owned"), pool.cursor() as cur:
            await cur.execute(insert_row, {"name": name, "data": Jsonb(payload)})
            row = await cur.fetchone()
            if row is None:
                msg = f"connections: row {name!r} was not saved"
                raise ConnectionStoreError(msg)

            connection_id = UUID(str(row[0]))
            await cur.execute(
                insert_grant,
                self._grant_params(connection_id, GrantTarget.user(user_id)),
            )

        return connection_id

    async def update(
        self, connection_id: UUID, name: str, profile: ConnectionProfile
    ) -> bool:
        """Полная замена имени и профиля; False — строки не было."""
        payload = self._cipher.encrypt(profile)
        query = self._sql(
            """
            update
                {connections}
            set
                {c_name} = %(name)s,
                {c_data} = %(data)s
            where
                {c_id} = %(id)s
            """
        )
        params = {"id": connection_id, "name": name, "data": Jsonb(payload)}

        pool = await self._pool()
        async with self._guarded("update"), pool.cursor() as cur:
            await cur.execute(query, params)
            return cur.rowcount > 0

    async def owned_ids(self, user_id: UUID) -> frozenset[UUID]:
        """Соединения с личным грантом пользователя: их он правит и удаляет сам."""
        query = self._sql(
            """
            select
                {g_src_kind_id}
            from
                {grants}
            where 1=1
                and {g_src_kind} = %(src_kind)s
                and {g_tgt_kind} = %(tgt_kind)s
                and {g_tgt_kind_id} = %(user_id)s
            """
        )
        params = {
            "src_kind": GrantKind.CONNECTIONS.value,
            "tgt_kind": GrantKind.USERS.value,
            "user_id": user_id,
        }

        pool = await self._pool()
        async with self._guarded("owned_ids"), pool.cursor() as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()

        ids: set[UUID] = set()
        for row in rows:
            ids.add(UUID(str(row[0])))

        return frozenset(ids)

    async def get(self, connection_id: UUID) -> StoredConnection:
        query = self._sql(
            """
            select
                {c_id},
                {c_name},
                {c_data}
            from
                {connections}
            where
                {c_id} = %(id)s
            """
        )

        pool = await self._pool()
        async with self._guarded("get"), pool.dict_cursor() as cur:
            await cur.execute(query, {"id": connection_id})
            row = await cur.fetchone()

        if row is None:
            msg = f"connections: connection #{connection_id} not found"
            raise ConnectionNotFoundError(msg)

        return self._stored(row)

    async def list_all(self) -> Sequence[StoredConnection]:
        query = self._sql(
            """
            select
                {c_id},
                {c_name},
                {c_data}
            from
                {connections}
            order by
                {c_name}
            """
        )

        pool = await self._pool()
        async with self._guarded("list"), pool.dict_cursor() as cur:
            await cur.execute(query)
            rows = await cur.fetchall()

        return list(map(self._stored, rows))

    async def remove(self, connection_id: UUID) -> bool:
        """Удаляет строку вместе с её грантами; False — строки не было."""
        drop_grants = self._sql(
            """
            delete from
                {grants}
            where
                {g_src_kind} = %(src_kind)s
                and {g_src_kind_id} = %(id)s
            """
        )
        drop_row = self._sql(
            """
            delete from
                {connections}
            where
                {c_id} = %(id)s
            """
        )
        params = {"id": connection_id, "src_kind": GrantKind.CONNECTIONS.value}

        pool = await self._pool()
        async with self._guarded("remove"), pool.cursor() as cur:
            await cur.execute(drop_grants, params)
            await cur.execute(drop_row, params)
            return cur.rowcount > 0

    async def roles(self) -> Sequence[StoredRole]:
        query = self._sql(
            """
            select
                {r_role},
                {r_id}
            from
                {roles}
            order by
                {r_role}
            """
        )

        pool = await self._pool()
        async with self._guarded("roles"), pool.cursor() as cur:
            await cur.execute(query)
            fetched = await cur.fetchall()

        roles: list[StoredRole] = []
        for row in fetched:
            roles.append(StoredRole(id=UUID(str(row[1])), name=row[0]))

        return roles

    async def grant(self, connection_id: UUID, target: GrantTarget) -> UUID:
        query = self._sql(
            """
            insert into {grants} (
                {g_src_kind},
                {g_src_kind_id},
                {g_tgt_kind},
                {g_tgt_kind_id}
            )
            values (
                %(src_kind)s,
                %(src_kind_id)s,
                %(tgt_kind)s,
                %(tgt_kind_id)s
            )
            on conflict ({g_src_kind}, {g_src_kind_id}, {g_tgt_kind}, {g_tgt_kind_id})
                do update set {g_src_kind} = excluded.{g_src_kind}
            returning
                {g_id}
            """
        )
        params = self._grant_params(connection_id, target)

        pool = await self._pool()
        async with self._guarded("grant"), pool.cursor() as cur:
            await cur.execute(query, params)
            row = await cur.fetchone()

        if row is None:
            msg = (
                f"grants: link connections#{connection_id} -> "
                f"{target.kind.value}#{target.id} was not saved"
            )
            raise ConnectionStoreError(msg)

        return UUID(str(row[0]))

    async def revoke(self, connection_id: UUID, target: GrantTarget) -> bool:
        query = self._sql(
            """
            delete from
                {grants}
            where
                {g_src_kind} = %(src_kind)s
                and {g_src_kind_id} = %(src_kind_id)s
                and {g_tgt_kind} = %(tgt_kind)s
                and {g_tgt_kind_id} = %(tgt_kind_id)s
            """
        )
        params = self._grant_params(connection_id, target)

        pool = await self._pool()
        async with self._guarded("revoke"), pool.cursor() as cur:
            await cur.execute(query, params)
            return cur.rowcount > 0

    async def grants_of(self, connection_id: UUID) -> Sequence[GrantTarget]:
        query = self._sql(
            """
            select
                {g_tgt_kind},
                {g_tgt_kind_id}
            from
                {grants}
            where
                {g_src_kind} = %(src_kind)s
                and {g_src_kind_id} = %(src_kind_id)s
            order by
                {g_tgt_kind},
                {g_tgt_kind_id}
            """
        )
        params = {
            "src_kind": GrantKind.CONNECTIONS.value,
            "src_kind_id": connection_id,
        }

        pool = await self._pool()
        async with self._guarded("grants"), pool.cursor() as cur:
            await cur.execute(query, params)
            fetched = await cur.fetchall()

        targets: list[GrantTarget] = []
        for row in fetched:
            targets.append(GrantTarget(kind=GrantKind(row[0]), id=UUID(str(row[1]))))

        return targets

    async def for_subject(
        self, subject: Subject, kind: ConnectionKind
    ) -> Sequence[StoredConnection]:
        """Соединения вида kind, выданные пользователю лично или любой его роли."""
        query = self._sql(
            """
            with
                subject_roles as (
                    select
                        r.{r_id}
                    from
                        {roles} r
                    where
                        r.{r_role} = any(%(roles)s)
                ),
                user_grants as (
                    select
                        g.{g_src_kind_id} as connection_id
                    from
                        {grants} g
                    where
                        g.{g_src_kind} = %(src_kind)s
                        and g.{g_tgt_kind} = %(users_kind)s
                        and g.{g_tgt_kind_id} = %(user_id)s
                ),
                role_grants as (
                    select
                        g.{g_src_kind_id} as connection_id
                    from
                        {grants} g
                        inner join subject_roles sr on
                            g.{g_tgt_kind_id} = sr.{r_id}
                    where
                        g.{g_src_kind} = %(src_kind)s
                        and g.{g_tgt_kind} = %(roles_kind)s
                ),
                granted as (
                    select
                        connection_id
                    from
                        user_grants
                    union
                    select
                        connection_id
                    from
                        role_grants
                )
            select
                c.{c_id},
                c.{c_name},
                c.{c_data}
            from
                {connections} c
                inner join granted on granted.connection_id = c.{c_id}
            where
                c.{c_data} ->> 'kind' = %(kind)s
            order by
                c.{c_name}
            """
        )
        params = {
            "kind": kind.value,
            "src_kind": GrantKind.CONNECTIONS.value,
            "users_kind": GrantKind.USERS.value,
            "roles_kind": GrantKind.ROLES.value,
            "user_id": subject.user_id,
            "roles": sorted(subject.roles),
        }

        pool = await self._pool()
        async with self._guarded("for subject"), pool.dict_cursor() as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()

        return list(map(self._stored, rows))

    @staticmethod
    def _grant_params(connection_id: UUID, target: GrantTarget) -> dict[str, Any]:
        return {
            "src_kind": GrantKind.CONNECTIONS.value,
            "src_kind_id": connection_id,
            "tgt_kind": target.kind.value,
            "tgt_kind_id": target.id,
        }

    def _stored(self, row: dict[str, Any]) -> StoredConnection:
        try:
            profile = self._PROFILE.validate_python(self._cipher.decrypt(row["data"]))
        except ValidationError as exc:
            # from None: в input_value разобранной строки лежит пароль, а
            # traceback печатает причину сам, мимо FailureText
            details = ValidationText.of(exc)
            msg = (
                f"connections: row #{row['id']} {row['name']!r} is not a valid "
                f"connection profile: {details}"
            )
            raise ConnectionStoreError(msg) from None

        return StoredConnection(
            id=UUID(str(row["id"])), name=row["name"], profile=profile
        )
