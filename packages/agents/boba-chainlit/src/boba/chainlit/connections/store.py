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
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Annotated, Any, ClassVar, TypeAlias

import psycopg
from psycopg import sql
from psycopg.errors import InsufficientPrivilege
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

from boba.chainlit.connections.secrets import SecretCipher
from boba.chainlit.domain.context import Subject
from boba.db.clickhouse import ClickHouseConfig
from boba.db.postgres import AsyncPostgresPool, PostgresConfig, PostgresError
from boba.toolkit.failure import ValidationText
from boba.transport.http import HttpProfile

logger = logging.getLogger(__name__)

__all__ = [
    "ConnectionKind",
    "ConnectionNotFoundError",
    "ConnectionProfile",
    "ConnectionStore",
    "ConnectionStoreError",
    "ConnectionsConfig",
    "GrantKind",
    "GrantTarget",
    "StoredConnection",
]


class ConnectionStoreError(Exception):
    """База отказала или строка не сохранилась."""


class ConnectionNotFoundError(ConnectionStoreError):
    """В таблице connections нет строки с таким id."""


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
        default="chainlit",
        description="Схема postgres, в которой живут таблицы.",
    )
    table: str = Field(
        default="connections",
        description="Имя таблицы соединений.",
    )
    roles_table: str = Field(
        default="roles",
        description="Имя таблицы ролей.",
    )
    grants_table: str = Field(
        default="grants",
        description="Имя связочной таблицы «источник — субъект» (src → tgt).",
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


ConnectionProfile: TypeAlias = Annotated[
    PostgresConfig | ClickHouseConfig | HttpProfile,
    Field(discriminator="kind"),
]
"""Рабочая модель соединения; jsonb строки разбирается по полю kind."""


class ConnectionKind(StrEnum):
    """Виды соединений: значение — дискриминатор kind рабочей модели."""

    POSTGRES = "postgres"
    CLICKHOUSE = "clickhouse"
    WEB = "web"

    @classmethod
    def of(cls, profile: ConnectionProfile) -> ConnectionKind:
        return cls(profile.kind)


class GrantKind(StrEnum):
    """Стороны связи в grants: значение — имя таблицы, kind_id — id в ней."""

    CONNECTIONS = "connections"
    ROLES = "roles"
    USERS = "users"


class GrantTarget(BaseModel):
    """Кому выдано соединение: пользователю или роли по id в их таблице."""

    model_config = ConfigDict(frozen=True)

    kind: GrantKind
    id: int

    @field_validator("kind")
    @classmethod
    def _target_only(cls, value: GrantKind) -> GrantKind:
        if value is GrantKind.CONNECTIONS:
            msg = "grant target must be a user or a role, not a connection"
            raise ValueError(msg)

        return value

    @classmethod
    def user(cls, user_id: int) -> GrantTarget:
        return cls(kind=GrantKind.USERS, id=user_id)

    @classmethod
    def role(cls, role_id: int) -> GrantTarget:
        return cls(kind=GrantKind.ROLES, id=role_id)


class StoredConnection(BaseModel):
    """Строка connections с расшифрованным профилем."""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    profile: ConnectionProfile

    @property
    def kind(self) -> ConnectionKind:
        return ConnectionKind.of(self.profile)


class ConnectionStore:
    """CRUD над connections/roles/grants: наружу — модели, в базе — шифротекст."""

    _PROFILE: ClassVar[TypeAdapter[ConnectionProfile]] = TypeAdapter(ConnectionProfile)

    def __init__(
        self,
        cfg: ConnectionsConfig,
        pool: AsyncPostgresPool | None = None,
    ) -> None:
        self._cfg = cfg
        self._pool_ref = pool
        self._cipher = SecretCipher(cfg.key_bytes())

    async def _pool(self) -> AsyncPostgresPool:
        """Пул берётся при первом обращении: __init__ не может await."""
        if self._pool_ref is None:
            self._pool_ref = await AsyncPostgresPool.get(self._cfg.require_conn())

        return self._pool_ref

    def _table(self) -> sql.Identifier:
        return sql.Identifier(self._cfg.db_schema, self._cfg.table)

    def _roles(self) -> sql.Identifier:
        return sql.Identifier(self._cfg.db_schema, self._cfg.roles_table)

    def _grants(self) -> sql.Identifier:
        return sql.Identifier(self._cfg.db_schema, self._cfg.grants_table)

    @asynccontextmanager
    async def _guarded(self, action: str) -> AsyncIterator[None]:
        """Граница слоя: отказ базы или пула уходит наружу как ConnectionStoreError."""
        try:
            yield
        except (psycopg.Error, PostgresError) as exc:
            msg = f"connections: {action} failed"
            raise ConnectionStoreError(msg) from exc

    async def setup(self) -> None:
        pool = await self._pool()

        async with self._guarded("setup"), pool.connection() as conn:
            try:
                await conn.execute(
                    sql.SQL("create schema if not exists {schema}").format(
                        schema=sql.Identifier(self._cfg.db_schema),
                    ),
                    prepare=False,
                )
            except InsufficientPrivilege:
                logger.info(
                    "no permission for create schema %r, "
                    "assuming an administrator created it",
                    self._cfg.db_schema,
                )

            for query in self._ddl():
                await conn.execute(query, prepare=False)

        logger.info(
            "connections ready: %s.%s",
            self._cfg.db_schema,
            self._cfg.table,
        )

    def _ddl(self) -> tuple[sql.Composed, ...]:
        return (
            sql.SQL(
                """
                create table if not exists {connections} (
                    id   integer generated always as identity primary key,
                    name text not null,
                    data jsonb not null default '{{}}'::jsonb
                )
                """
            ).format(
                connections=self._table(),
            ),
            sql.SQL(
                """
                create index if not exists idx_connections_kind
                    on {connections} ((data ->> 'kind'))
                """
            ).format(
                connections=self._table(),
            ),
            sql.SQL(
                """
                create table if not exists {roles} (
                    id        integer generated always as identity primary key,
                    role      varchar not null unique,
                    create_at timestamptz not null default now()
                )
                """
            ).format(
                roles=self._roles(),
            ),
            sql.SQL(
                """
                create table if not exists {grants} (
                    id          integer generated always as identity primary key,
                    src_kind    varchar not null,
                    src_kind_id integer not null,
                    tgt_kind    varchar not null,
                    tgt_kind_id integer not null,
                    unique (src_kind, src_kind_id, tgt_kind, tgt_kind_id)
                )
                """
            ).format(
                grants=self._grants(),
            ),
            sql.SQL(
                """
                create index if not exists idx_grants_target
                    on {grants} (tgt_kind, tgt_kind_id)
                """
            ).format(
                grants=self._grants(),
            ),
        )

    async def sync_roles(self, names: Iterable[str]) -> None:
        """Добавляет в roles имена, которых там ещё нет; ничего не удаляет."""
        query = sql.SQL(
            """
            insert into {roles} (
                role
            )
            values (
                %(role)s
            )
            on conflict (role) do nothing
            """
        ).format(
            roles=self._roles(),
        )

        rows: list[dict[str, str]] = []
        for name in names:
            rows.append({"role": name})

        if not rows:
            return

        pool = await self._pool()
        async with self._guarded("sync roles"), pool.cursor() as cur:
            await cur.executemany(query, rows)

    async def add(self, name: str, profile: ConnectionProfile) -> int:
        """Новая строка connections; уникальность имени — забота вызывающего."""
        payload = self._cipher.encrypt(profile)
        query = sql.SQL(
            """
            insert into {connections} (
                name,
                data
            )
            values (
                %(name)s,
                %(data)s
            )
            returning
                id
            """
        ).format(
            connections=self._table(),
        )
        params = {"name": name, "data": Jsonb(payload)}

        pool = await self._pool()
        async with self._guarded("add"), pool.cursor() as cur:
            await cur.execute(query, params)
            row = await cur.fetchone()

        if row is None:
            msg = f"connections: row {name!r} was not saved"
            raise ConnectionStoreError(msg)

        return int(row[0])

    async def get(self, connection_id: int) -> StoredConnection:
        query = sql.SQL(
            """
            select
                id,
                name,
                data
            from
                {connections}
            where
                id = %(id)s
            """
        ).format(
            connections=self._table(),
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
        query = sql.SQL(
            """
            select
                id,
                name,
                data
            from
                {connections}
            order by
                id
            """
        ).format(
            connections=self._table(),
        )

        pool = await self._pool()
        async with self._guarded("list"), pool.dict_cursor() as cur:
            await cur.execute(query)
            rows = await cur.fetchall()

        return list(map(self._stored, rows))

    async def remove(self, connection_id: int) -> bool:
        """Удаляет строку вместе с её грантами; False — строки не было."""
        drop_grants = sql.SQL(
            """
            delete from
                {grants}
            where
                src_kind = %(src_kind)s
                and src_kind_id = %(id)s
            """
        ).format(
            grants=self._grants(),
        )
        drop_row = sql.SQL(
            """
            delete from
                {connections}
            where
                id = %(id)s
            """
        ).format(
            connections=self._table(),
        )
        params = {"id": connection_id, "src_kind": GrantKind.CONNECTIONS.value}

        pool = await self._pool()
        async with self._guarded("remove"), pool.cursor() as cur:
            await cur.execute(drop_grants, params)
            await cur.execute(drop_row, params)
            return cur.rowcount > 0

    async def roles(self) -> dict[str, int]:
        query = sql.SQL(
            """
            select
                role,
                id
            from
                {roles}
            order by
                id
            """
        ).format(
            roles=self._roles(),
        )

        pool = await self._pool()
        async with self._guarded("roles"), pool.cursor() as cur:
            await cur.execute(query)
            fetched = await cur.fetchall()

        by_name: dict[str, int] = {}
        for row in fetched:
            by_name[row[0]] = int(row[1])

        return by_name

    async def grant(self, connection_id: int, target: GrantTarget) -> int:
        query = sql.SQL(
            """
            insert into {grants} (
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
            on conflict (src_kind, src_kind_id, tgt_kind, tgt_kind_id) do update set
                src_kind = excluded.src_kind
            returning
                id
            """
        ).format(
            grants=self._grants(),
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

        return int(row[0])

    async def revoke(self, connection_id: int, target: GrantTarget) -> bool:
        query = sql.SQL(
            """
            delete from
                {grants}
            where
                src_kind = %(src_kind)s
                and src_kind_id = %(src_kind_id)s
                and tgt_kind = %(tgt_kind)s
                and tgt_kind_id = %(tgt_kind_id)s
            """
        ).format(
            grants=self._grants(),
        )
        params = self._grant_params(connection_id, target)

        pool = await self._pool()
        async with self._guarded("revoke"), pool.cursor() as cur:
            await cur.execute(query, params)
            return cur.rowcount > 0

    async def grants_of(self, connection_id: int) -> Sequence[GrantTarget]:
        query = sql.SQL(
            """
            select
                tgt_kind,
                tgt_kind_id
            from
                {grants}
            where
                src_kind = %(src_kind)s
                and src_kind_id = %(src_kind_id)s
            order by
                id
            """
        ).format(
            grants=self._grants(),
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
            targets.append(GrantTarget(kind=GrantKind(row[0]), id=int(row[1])))

        return targets

    async def for_subject(
        self, subject: Subject, kind: ConnectionKind
    ) -> Sequence[StoredConnection]:
        """Соединения вида kind, выданные пользователю лично или любой его роли."""
        query = sql.SQL(
            """
            with
                subject_roles as (
                    select
                        r.id
                    from
                        {roles} r
                    where
                        r.role = any(%(roles)s)
                ),
                user_grants as (
                    select
                        g.src_kind_id as connection_id
                    from
                        {grants} g
                    where
                        g.src_kind = %(src_kind)s
                        and g.tgt_kind = %(users_kind)s
                        and g.tgt_kind_id = %(user_id)s
                ),
                role_grants as (
                    select
                        g.src_kind_id as connection_id
                    from
                        {grants} g
                        inner join subject_roles sr on
                            g.tgt_kind_id = sr.id
                    where
                        g.src_kind = %(src_kind)s
                        and g.tgt_kind = %(roles_kind)s
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
                c.id,
                c.name,
                c.data
            from
                {connections} c
                inner join granted on granted.connection_id = c.id
            where
                c.data ->> 'kind' = %(kind)s
            order by
                c.id
            """
        ).format(
            connections=self._table(),
            grants=self._grants(),
            roles=self._roles(),
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
    def _grant_params(connection_id: int, target: GrantTarget) -> dict[str, Any]:
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

        return StoredConnection(id=int(row["id"]), name=row["name"], profile=profile)
