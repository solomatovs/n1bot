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
        default="connection_grants",
        description="Имя связочной таблицы «соединение — роль/пользователь».",
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
            msg = "connections.encryption_key: ожидается base64"
            raise ValueError(msg) from e
        if len(decoded) != KEY_BYTES:
            msg = (
                f"connections.encryption_key: нужен ключ {KEY_BYTES} байт, "
                f"получено {len(decoded)}"
            )
            raise ValueError(msg)
        return value

    def key_bytes(self) -> bytes:
        raw = self.encryption_key.get_secret_value()
        if not raw:
            msg = "connections.encryption_key не задан"
            raise ValueError(msg)
        return base64.b64decode(raw, validate=True)

    def require_conn(self) -> PostgresConfig:
        if self.connection is None:
            msg = 'connections.connection не задан: connection = "${postgres}"'
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
                f"неизвестный kind соединения {kind!r} "
                f"(известны: {', '.join(cls.known())})"
            )
            raise ValueError(msg) from None

    @classmethod
    def kind_of(cls, profile: BaseModel) -> str:
        kind = getattr(profile, "kind", None)
        if not isinstance(kind, str):
            msg = f"{type(profile).__name__}: нет поля kind — это не профиль соединения"
            raise ValueError(msg)
        return kind

    @classmethod
    def known(cls) -> tuple[str, ...]:
        return tuple(sorted(cls._BY_KIND))


class GrantKinds:
    """Виды субъектов в связочной таблице: kind — имя таблицы для kind_id."""

    ROLES: ClassVar[str] = "roles"
    USERS: ClassVar[str] = "users"

    @classmethod
    def known(cls) -> tuple[str, ...]:
        return (cls.ROLES, cls.USERS)

    @classmethod
    def validate(cls, kind: str) -> str:
        if kind not in cls.known():
            msg = (
                f"неизвестный kind связи {kind!r} "
                f"(известны: {', '.join(cls.known())})"
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
                    "нет прав на create schema %r, считаем что её создал администратор",
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
                    id            integer generated always as identity primary key,
                    connection_id integer not null
                                  references {connections} (id) on delete cascade,
                    kind          varchar not null,
                    kind_id       integer not null,
                    unique (connection_id, kind, kind_id)
                )
                """
            ).format(
                grants=self._grants(),
                connections=self._table(),
            ),
            sql.SQL(
                """
                create index if not exists idx_connection_grants_subject
                    on {grants} (kind, kind_id)
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
            msg = f"connections: строка {name!r} не сохранена"
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
            msg = f"connections: соединение {name!r} не найдено"
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
        query = sql.SQL(
            """
            delete from
                {connections}
            where
                name = %s
            """
        ).format(
            connections=self._table(),
        )

        with self._pool.cursor() as cur:
            cur.execute(query, (name,))
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

    def grant(self, name: str, kind: str, kind_id: int) -> int:
        GrantKinds.validate(kind)
        query = sql.SQL(
            """
            insert into {grants}
                (connection_id, kind, kind_id)
            select
                c.id, %(kind)s, %(kind_id)s
            from
                {connections} c
            where
                c.name = %(name)s
            on conflict
                (connection_id, kind, kind_id)
            do update set
                kind = excluded.kind
            returning
                id
            """
        ).format(
            grants=self._grants(),
            connections=self._table(),
        )

        with self._pool.cursor() as cur:
            cur.execute(query, {"name": name, "kind": kind, "kind_id": kind_id})
            row = cur.fetchone()

        if row is None:
            msg = f"connections: соединение {name!r} не найдено"
            raise ConnectionNotFoundError(msg)
        return int(row[0])

    def revoke(self, name: str, kind: str, kind_id: int) -> bool:
        GrantKinds.validate(kind)
        query = sql.SQL(
            """
            delete from
                {grants} g
            using
                {connections} c
            where
                g.connection_id = c.id
                and c.name  = %(name)s
                and g.kind  = %(kind)s
                and g.kind_id = %(kind_id)s
            """
        ).format(
            grants=self._grants(),
            connections=self._table(),
        )

        with self._pool.cursor() as cur:
            cur.execute(query, {"name": name, "kind": kind, "kind_id": kind_id})
            return cur.rowcount > 0

    def grants_of(self, name: str) -> list[tuple[str, int]]:
        query = sql.SQL(
            """
            select
                g.kind, g.kind_id
            from
                {grants} g
                inner join {connections} c on g.connection_id = c.id
            where
                c.name = %s
            order by
                g.id
            """
        ).format(
            grants=self._grants(),
            connections=self._table(),
        )

        with self._pool.cursor() as cur:
            cur.execute(query, (name,))
            return [(row[0], int(row[1])) for row in cur.fetchall()]

    def connections_for(self, kind: str, kind_id: int) -> dict[str, BaseModel]:
        GrantKinds.validate(kind)
        query = sql.SQL(
            """
            select
                c.name, c.kind, c.data
            from
                {connections} c
                inner join {grants} g on g.connection_id = c.id
            where
                g.kind = %(kind)s
                and g.kind_id = %(kind_id)s
            order by
                c.id
            """
        ).format(
            connections=self._table(),
            grants=self._grants(),
        )

        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, {"kind": kind, "kind_id": kind_id})
            rows = cur.fetchall()

        return {row["name"]: self._to_profile(row["kind"], row["data"]) for row in rows}

    def _to_profile(self, kind: str, data: dict[str, Any]) -> BaseModel:
        model = ConnectionKinds.model(kind)
        return model.model_validate(self._cipher.decrypt(data))
