"""Таблица connections: хранение профилей подключений с шифрованием секретов."""

from __future__ import annotations

import base64
import binascii
import logging
from typing import Any, ClassVar

from psycopg import sql
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from boba.chainlit2.connections.secrets import SecretCipher
from boba.db.postgres import PostgresConfig, PostgresPool
from boba.transport.http import HttpProfile

logger = logging.getLogger(__name__)

__all__ = [
    "ConnectionKinds",
    "ConnectionNotFoundError",
    "ConnectionStore",
    "ConnectionsConfig",
    "GrantKinds",
]

KEY_BYTES = 32


class ConnectionsConfig(BaseModel):
    """Секция [connections]: где лежит таблица и чем шифруются значения."""

    model_config = ConfigDict(extra="ignore")

    enable: bool = Field(
        default=False,
        description="Создавать таблицу connections при старте приложения.",
    )
    connection: PostgresConfig | None = Field(
        default=None,
        description='Postgres-профиль ссылкой: connection = "${postgres}".',
    )
    db_schema: str = Field(
        default="chainlit",
        description="Схема postgres, в которой живёт таблица connections.",
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
            "python -c \"import base64,secrets;"
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
        if len(decoded) != KEY_BYTES:
            msg = (
                f"connections.encryption_key: {KEY_BYTES}-byte key required, "
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


class ConnectionKinds:
    """Реестр kind -> рабочая модель профиля; по нему валидируется jsonb."""

    _BY_KIND: ClassVar[dict[str, type[BaseModel]]] = {
        "postgres": PostgresConfig,
        "web": HttpProfile,
    }

    @classmethod
    def model(cls, kind: str) -> type[BaseModel]:
        try:
            return cls._BY_KIND[kind]
        except KeyError:
            msg = (
                f"unknown connection kind {kind!r} "
                f"(known: {', '.join(cls.known())})"
            )
            raise ValueError(msg) from None

    @classmethod
    def kind_of(cls, profile: BaseModel) -> str:
        kind = getattr(profile, "kind", None)
        if not isinstance(kind, str):
            msg = f"{type(profile).__name__}: no kind field, not a connection profile"
            raise ValueError(msg)
        return kind

    @classmethod
    def known(cls) -> tuple[str, ...]:
        return tuple(sorted(cls._BY_KIND))


class GrantKinds:
    """Стороны связи в таблице grants: kind — имя таблицы, kind_id — id в ней."""

    CONNECTIONS: ClassVar[str] = "connections"
    ROLES: ClassVar[str] = "roles"
    USERS: ClassVar[str] = "users"

    @classmethod
    def known_src(cls) -> tuple[str, ...]:
        return (cls.CONNECTIONS,)

    @classmethod
    def known_tgt(cls) -> tuple[str, ...]:
        return (cls.ROLES, cls.USERS)

    @classmethod
    def validate_src(cls, kind: str) -> str:
        if kind not in cls.known_src():
            msg = (
                f"unknown grant src_kind {kind!r} "
                f"(known: {', '.join(cls.known_src())})"
            )
            raise ValueError(msg)
        return kind

    @classmethod
    def validate_tgt(cls, kind: str) -> str:
        if kind not in cls.known_tgt():
            msg = (
                f"unknown grant tgt_kind {kind!r} "
                f"(known: {', '.join(cls.known_tgt())})"
            )
            raise ValueError(msg)
        return kind


class ConnectionNotFoundError(LookupError):
    """В таблице connections нет строки с таким именем."""


class ConnectionStore:
    """CRUD над таблицей connections: наружу — профили, в базе — шифротекст."""

    def __init__(
        self,
        cfg: ConnectionsConfig,
        pool: PostgresPool | None = None,
    ) -> None:
        self._cfg = cfg
        self._pool = pool if pool is not None else PostgresPool.get(cfg.require_conn())
        self._cipher = SecretCipher(cfg.key_bytes())

    def _table(self) -> sql.Identifier:
        return sql.Identifier(self._cfg.db_schema, self._cfg.table)

    def _roles(self) -> sql.Identifier:
        return sql.Identifier(self._cfg.db_schema, self._cfg.roles_table)

    def _grants(self) -> sql.Identifier:
        return sql.Identifier(self._cfg.db_schema, self._cfg.grants_table)

    def setup(self) -> None:
        with self._pool.connection() as conn:
            try:
                conn.execute(
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
                conn.execute(query, prepare=False)

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
                    name text not null unique,
                    kind text not null,
                    data jsonb not null default '{{}}'::jsonb
                )
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

    def save(self, name: str, profile: BaseModel) -> int:
        kind = ConnectionKinds.kind_of(profile)
        payload = self._cipher.encrypt(profile)
        query = sql.SQL(
            """
            insert into {connections}
                (name, kind, data)
            values
                (%(name)s, %(kind)s, %(data)s)
            on conflict
                (name)
            do update set
                kind = excluded.kind,
                data = excluded.data
            returning
                id
            """
        ).format(
            connections=self._table(),
        )

        with self._pool.cursor() as cur:
            cur.execute(query, {"name": name, "kind": kind, "data": Jsonb(payload)})
            row = cur.fetchone()

        if row is None:
            msg = f"connections: row {name!r} was not saved"
            raise RuntimeError(msg)
        return int(row[0])

    def load(self, name: str) -> BaseModel:
        query = sql.SQL(
            """
            select
                kind, data
            from
                {connections}
            where
                name = %s
            limit
                1
            """
        ).format(
            connections=self._table(),
        )

        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, (name,))
            row = cur.fetchone()

        if row is None:
            msg = f"connections: connection {name!r} not found"
            raise ConnectionNotFoundError(msg)
        return self._to_profile(row["kind"], row["data"])

    def load_all(self, kind: str | None = None) -> dict[str, BaseModel]:
        where = sql.SQL("where kind = %(kind)s") if kind else sql.SQL("")
        query = sql.SQL(
            """
            select
                name, kind, data
            from
                {connections}
            {where}
            order by
                id
            """
        ).format(
            connections=self._table(),
            where=where,
        )

        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, {"kind": kind})
            rows = cur.fetchall()

        return {row["name"]: self._to_profile(row["kind"], row["data"]) for row in rows}

    def delete(self, name: str) -> bool:
        drop_grants = sql.SQL(
            """
            delete from
                {grants} g
            using
                {connections} c
            where
                c.name = %(name)s
                and g.src_kind = %(src_kind)s
                and g.src_kind_id = c.id
            """
        ).format(
            grants=self._grants(),
            connections=self._table(),
        )
        drop_row = sql.SQL(
            """
            delete from
                {connections}
            where
                name = %(name)s
            """
        ).format(
            connections=self._table(),
        )

        with self._pool.connection() as conn, conn.cursor() as cur:
            params = {"name": name, "src_kind": GrantKinds.CONNECTIONS}
            cur.execute(drop_grants, params)
            cur.execute(drop_row, params)
            return cur.rowcount > 0

    def roles(self) -> dict[str, int]:
        query = sql.SQL(
            """
            select
                role, id
            from
                {roles}
            order by
                id
            """
        ).format(
            roles=self._roles(),
        )

        with self._pool.cursor() as cur:
            cur.execute(query)
            return {row[0]: int(row[1]) for row in cur.fetchall()}

    def grant(
        self,
        src_kind: str,
        src_kind_id: int,
        tgt_kind: str,
        tgt_kind_id: int,
    ) -> int:
        GrantKinds.validate_src(src_kind)
        GrantKinds.validate_tgt(tgt_kind)
        query = sql.SQL(
            """
            insert into {grants}
                (src_kind, src_kind_id, tgt_kind, tgt_kind_id)
            values
                (%(src_kind)s, %(src_kind_id)s, %(tgt_kind)s, %(tgt_kind_id)s)
            on conflict
                (src_kind, src_kind_id, tgt_kind, tgt_kind_id)
            do update set
                src_kind = excluded.src_kind
            returning
                id
            """
        ).format(
            grants=self._grants(),
        )
        params = {
            "src_kind": src_kind,
            "src_kind_id": src_kind_id,
            "tgt_kind": tgt_kind,
            "tgt_kind_id": tgt_kind_id,
        }

        with self._pool.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()

        if row is None:
            msg = (
                f"grants: link {src_kind}#{src_kind_id} -> "
                f"{tgt_kind}#{tgt_kind_id} was not saved"
            )
            raise RuntimeError(msg)
        return int(row[0])

    def revoke(
        self,
        src_kind: str,
        src_kind_id: int,
        tgt_kind: str,
        tgt_kind_id: int,
    ) -> bool:
        GrantKinds.validate_src(src_kind)
        GrantKinds.validate_tgt(tgt_kind)
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
        params = {
            "src_kind": src_kind,
            "src_kind_id": src_kind_id,
            "tgt_kind": tgt_kind,
            "tgt_kind_id": tgt_kind_id,
        }

        with self._pool.cursor() as cur:
            cur.execute(query, params)
            return cur.rowcount > 0

    def grants_of(self, src_kind: str, src_kind_id: int) -> list[tuple[str, int]]:
        GrantKinds.validate_src(src_kind)
        query = sql.SQL(
            """
            select
                tgt_kind, tgt_kind_id
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

        with self._pool.cursor() as cur:
            cur.execute(query, {"src_kind": src_kind, "src_kind_id": src_kind_id})
            return [(row[0], int(row[1])) for row in cur.fetchall()]

    def grant_connection(self, name: str, tgt_kind: str, tgt_kind_id: int) -> int:
        connection_id = self._connection_id(name)
        return self.grant(GrantKinds.CONNECTIONS, connection_id, tgt_kind, tgt_kind_id)

    def revoke_connection(self, name: str, tgt_kind: str, tgt_kind_id: int) -> bool:
        connection_id = self._connection_id(name)
        return self.revoke(GrantKinds.CONNECTIONS, connection_id, tgt_kind, tgt_kind_id)

    def connection_grants(self, name: str) -> list[tuple[str, int]]:
        connection_id = self._connection_id(name)
        return self.grants_of(GrantKinds.CONNECTIONS, connection_id)

    def connections_for(self, tgt_kind: str, tgt_kind_id: int) -> dict[str, BaseModel]:
        GrantKinds.validate_tgt(tgt_kind)
        query = sql.SQL(
            """
            select
                c.name, c.kind, c.data
            from
                {connections} c
                inner join {grants} g on
                    g.src_kind = %(src_kind)s
                    and g.src_kind_id = c.id
            where
                g.tgt_kind = %(tgt_kind)s
                and g.tgt_kind_id = %(tgt_kind_id)s
            order by
                c.id
            """
        ).format(
            connections=self._table(),
            grants=self._grants(),
        )
        params = {
            "src_kind": GrantKinds.CONNECTIONS,
            "tgt_kind": tgt_kind,
            "tgt_kind_id": tgt_kind_id,
        }

        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

        return {row["name"]: self._to_profile(row["kind"], row["data"]) for row in rows}

    def _connection_id(self, name: str) -> int:
        query = sql.SQL(
            """
            select
                id
            from
                {connections}
            where
                name = %s
            limit
                1
            """
        ).format(
            connections=self._table(),
        )

        with self._pool.cursor() as cur:
            cur.execute(query, (name,))
            row = cur.fetchone()

        if row is None:
            msg = f"connections: connection {name!r} not found"
            raise ConnectionNotFoundError(msg)
        return int(row[0])

    def _to_profile(self, kind: str, data: dict[str, Any]) -> BaseModel:
        model = ConnectionKinds.model(kind)
        return model.model_validate(self._cipher.decrypt(data))
