"""Таблицы connections/roles/grants: профили соединений и кому они выданы.

connections — чистое хранилище профилей, без владельца и без уникальности
имени; связь с пользователями и ролями живёт только в grants.

Ошибки:
ConnectionStoreError — база отказала, строка не сохранилась или её jsonb
    не разбирается как профиль.
ConnectionNotFoundError — в connections нет строки с таким id.
UnknownConnectionKindError — точечное чтение строки типа, чей пакет не установлен;
    списки такие строки пропускают с warning.
SecretCryptoError — секрет строки не расшифровался ключом конфига.
"""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any, ClassVar
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
)

from boba.access.grants import ConnectionFilter, SubjectGrantsQuery, SubjectRowColumn
from boba.connections.manifest import (
    ConnectionTypes,
    ConnectionTypesError,
    UnknownConnectionKindError,
)
from boba.connections.secrets import SecretCipher
from boba.connections.stored import (
    ConnectionBase,
    ConnectionNotFoundError,
    ConnectionRepository,
    ConnectionsColumn,
    ConnectionStoreError,
    GrantedConnection,
    GrantKind,
    GrantsColumn,
    GrantTarget,
    MissingTypeConnection,
    RolesColumn,
    StoredConnection,
    StoredRole,
    SubjectConnections,
)
from boba.db.postgres import PgQuery, PostgresPool, PostgresTable
from boba.db.postgres.connection import PostgresConfig
from boba.identity.context import Subject

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
            msg = (
                f"[connections].encryption_key: expected {cls.KEY_BYTES} bytes "
                f"in base64, got a value that does not decode: {e}"
            )
            raise ValueError(msg) from e

        if len(decoded) != cls.KEY_BYTES:
            msg = (
                f"[connections].encryption_key: expected {cls.KEY_BYTES} bytes "
                f"in base64, got {len(decoded)} bytes"
            )
            raise ValueError(msg)

        return value

    def key_bytes(self) -> bytes:
        raw = self.encryption_key.get_secret_value()
        if not raw:
            msg = (
                "[connections].encryption_key is not set: expected "
                f"{self.KEY_BYTES} bytes in base64 to encrypt stored profiles"
            )
            raise ValueError(msg)

        return base64.b64decode(raw, validate=True)

    def require_conn(self) -> PostgresConfig:
        if self.connection is None:
            msg = (
                "[connections].connection is not set: expected a postgres "
                'reference such as connection = "${postgres}"'
            )
            raise ValueError(msg)

        return self.connection


class ConnectionStore(PostgresTable, ConnectionRepository):
    """CRUD над connections/roles/grants: наружу — модели, в базе — шифротекст.

    Профили разбираются реестром установленных типов соединений: строка с kind
    без пакета-владельца падает UnknownConnectionKindError при обращении.
    """

    LABEL: ClassVar[str] = "connections"

    def __init__(
        self,
        cfg: ConnectionsConfig,
        types: ConnectionTypes,
        pool: PostgresPool | None = None,
    ) -> None:
        postgres = cfg.connection
        if pool is None:
            postgres = cfg.require_conn()

        super().__init__(postgres, cfg.db_schema, pool)
        self._cfg = cfg
        self._types = types
        self._cipher = SecretCipher(cfg.key_bytes())

    def _failure(self, action: str, exc: Exception) -> Exception:
        return ConnectionStoreError(self._detail(action, exc))

    async def setup(self) -> None:
        """Схема и три таблицы; повтор безвреден."""
        ddl = (*self._connections_ddl(), *self._roles_ddl(), *self._grants_ddl())
        await self._apply_ddl(ddl)

        logger.info("connections ready: %s", self.schema)

    def _connections_ddl(self) -> tuple[PgQuery, ...]:
        return (
            self._query()
            .add(
                """
                create table if not exists {schema}.connections (
                    id   uuid primary key default gen_random_uuid(),
                    name text not null,
                    data jsonb not null default '{{}}'::jsonb
                )
                """
            )
            .build(),
            self._query()
            .add(
                """
                create index if not exists idx_connections_kind
                    on {schema}.connections ((data ->> 'kind'))
                """
            )
            .build(),
        )

    def _roles_ddl(self) -> tuple[PgQuery, ...]:
        return (
            self._query()
            .add(
                """
                create table if not exists {schema}.roles (
                    id        uuid primary key default gen_random_uuid(),
                    role      varchar not null unique,
                    create_at timestamptz not null default now()
                )
                """
            )
            .build(),
        )

    def _grants_ddl(self) -> tuple[PgQuery, ...]:
        return (
            self._query()
            .add(
                """
                create table if not exists {schema}.grants (
                    id          uuid primary key default gen_random_uuid(),
                    src_kind    varchar not null,
                    src_kind_id uuid not null,
                    tgt_kind    varchar not null,
                    tgt_kind_id uuid not null,
                    unique (src_kind, src_kind_id, tgt_kind, tgt_kind_id)
                )
                """
            )
            .build(),
            self._query()
            .add(
                """
                create index if not exists idx_grants_target
                    on {schema}.grants (tgt_kind, tgt_kind_id)
                """
            )
            .build(),
        )

    async def sync_roles(self, names: Iterable[str]) -> None:
        """Добавляет в roles имена, которых там ещё нет; ничего не удаляет."""
        rows: list[dict[str, str]] = []
        for name in names:
            rows.append({"role": name})

        if not rows:
            return

        query = (
            self._query()
            .add(
                """
                insert into {schema}.roles (
                    role
                )
                values (
                    %(role)s
                )
                on conflict (role) do nothing
                """
            )
            .build()
        )

        async with self._transaction("sync roles") as cur:
            await cur.executemany(query.text, rows)

    async def add(self, name: str, connection: ConnectionBase) -> UUID:
        """Новая строка connections; уникальность имени — забота вызывающего."""
        payload = self._cipher.encrypt(connection)
        query = (
            self._query()
            .add(
                """
                insert into {schema}.connections (
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
        row = self._returning(
            await self._row(query, "add"), f"insert of row {name!r} into connections"
        )

        return UUID(str(row[ConnectionsColumn.ID.value]))

    async def add_owned(
        self, name: str, connection: ConnectionBase, user_id: UUID
    ) -> UUID:
        """Строка и личный грант одной транзакцией: личный грант и есть владение."""
        payload = self._cipher.encrypt(connection)
        insert_row = (
            self._query()
            .add(
                """
                insert into {schema}.connections (
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

        async with self._transaction("add_owned") as cur:
            await cur.execute(insert_row.text, insert_row.params)
            row = self._returning(
                await cur.fetchone(), f"insert of row {name!r} into connections"
            )
            connection_id = UUID(str(row[ConnectionsColumn.ID.value]))
            insert_grant = self._grant_insert(connection_id, GrantTarget.user(user_id))
            await cur.execute(insert_grant.text, insert_grant.params)

        return connection_id

    async def update(
        self, connection_id: UUID, name: str, connection: ConnectionBase
    ) -> bool:
        """Полная замена имени и профиля; False — строки не было."""
        payload = self._cipher.encrypt(connection)
        query = (
            self._query()
            .add(
                """
                update
                    {schema}.connections
                set
                    name = %(name)s,
                    data = %(data)s
                where
                    id = %(id)s
                """,
                id=connection_id,
                name=name,
                data=Jsonb(payload),
            )
            .build()
        )
        touched = await self._execute(query, "update")

        return touched > 0

    async def owned_ids(self, user_id: UUID) -> frozenset[UUID]:
        """Соединения с личным грантом пользователя: их он правит и удаляет сам."""
        query = (
            self._query()
            .add(
                """
                select
                    src_kind_id
                from
                    {schema}.grants
                where 1=1
                    and src_kind = %(src_kind)s
                    and tgt_kind = %(tgt_kind)s
                    and tgt_kind_id = %(user_id)s
                """,
                src_kind=GrantKind.CONNECTIONS.value,
                tgt_kind=GrantKind.USERS.value,
                user_id=user_id,
            )
            .build()
        )
        rows = await self._rows(query, "owned_ids")

        ids: set[UUID] = set()
        for row in rows:
            ids.add(UUID(str(row[GrantsColumn.SRC_KIND_ID.value])))

        return frozenset(ids)

    async def get(self, connection_id: UUID) -> StoredConnection:
        query = (
            self._query()
            .add(
                """
                select
                    id,
                    name,
                    data
                from
                    {schema}.connections
                where
                    id = %(id)s
                """,
                id=connection_id,
            )
            .build()
        )
        row = await self._row(query, "get")
        if row is None:
            msg = (
                f"connections: connection #{connection_id} not found in "
                f"{self.schema}.connections"
            )
            raise ConnectionNotFoundError(msg)

        return self._stored(row)

    async def name_of(self, connection_id: UUID) -> str:
        """Имя строки без разбора профиля: живёт и у строк без типа."""
        query = (
            self._query()
            .add(
                """
                select
                    name
                from
                    {schema}.connections
                where
                    id = %(id)s
                """,
                id=connection_id,
            )
            .build()
        )
        row = await self._row(query, "name of")
        if row is None:
            msg = (
                f"connections: connection #{connection_id} not found in "
                f"{self.schema}.connections"
            )
            raise ConnectionNotFoundError(msg)

        return str(row[ConnectionsColumn.NAME.value])

    async def list_all(self) -> Sequence[StoredConnection]:
        query = (
            self._query()
            .add(
                """
                select
                    id,
                    name,
                    data
                from
                    {schema}.connections
                order by
                    name
                """
            )
            .build()
        )
        rows = await self._rows(query, "list")

        return self._stored_rows(rows)

    async def remove(self, connection_id: UUID) -> bool:
        """Удаляет строку вместе с её грантами; False — строки не было."""
        drop_grants = (
            self._query()
            .add(
                """
                delete from
                    {schema}.grants
                where
                    src_kind = %(src_kind)s
                    and src_kind_id = %(id)s
                """,
                id=connection_id,
                src_kind=GrantKind.CONNECTIONS.value,
            )
            .build()
        )
        drop_row = (
            self._query()
            .add(
                """
                delete from
                    {schema}.connections
                where
                    id = %(id)s
                """,
                id=connection_id,
            )
            .build()
        )

        async with self._transaction("remove") as cur:
            await cur.execute(drop_grants.text, drop_grants.params)
            await cur.execute(drop_row.text, drop_row.params)

            return cur.rowcount > 0

    async def roles(self) -> Sequence[StoredRole]:
        query = (
            self._query()
            .add(
                """
                select
                    role,
                    id
                from
                    {schema}.roles
                order by
                    role
                """
            )
            .build()
        )
        rows = await self._rows(query, "roles")

        roles: list[StoredRole] = []
        for row in rows:
            roles.append(
                StoredRole(
                    id=UUID(str(row[RolesColumn.ID.value])),
                    name=row[RolesColumn.ROLE.value],
                )
            )

        return roles

    async def grant(self, connection_id: UUID, target: GrantTarget) -> UUID:
        query = self._grant_insert(connection_id, target)
        row = self._returning(
            await self._row(query, "grant"),
            f"insert of link connections#{connection_id} -> "
            f"{target.kind.value}#{target.id} into grants",
        )

        return UUID(str(row[GrantsColumn.ID.value]))

    def _grant_insert(self, connection_id: UUID, target: GrantTarget) -> PgQuery:
        """Грант соединения цели; повтор той же связи ничего не меняет."""
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
                    do update set src_kind = excluded.src_kind
                returning
                    id
                """,
                **self._grant_params(connection_id, target),
            )
            .build()
        )

    async def revoke(self, connection_id: UUID, target: GrantTarget) -> bool:
        query = (
            self._query()
            .add(
                """
                delete from
                    {schema}.grants
                where
                    src_kind = %(src_kind)s
                    and src_kind_id = %(src_kind_id)s
                    and tgt_kind = %(tgt_kind)s
                    and tgt_kind_id = %(tgt_kind_id)s
                """,
                **self._grant_params(connection_id, target),
            )
            .build()
        )
        touched = await self._execute(query, "revoke")

        return touched > 0

    async def grants_of(self, connection_id: UUID) -> Sequence[GrantTarget]:
        query = (
            self._query()
            .add(
                """
                select
                    tgt_kind,
                    tgt_kind_id
                from
                    {schema}.grants
                where 1=1
                    and src_kind = %(src_kind)s
                    and src_kind_id = %(src_kind_id)s
                order by
                    tgt_kind,
                    tgt_kind_id
                """,
                src_kind=GrantKind.CONNECTIONS.value,
                src_kind_id=connection_id,
            )
            .build()
        )
        rows = await self._rows(query, "grants")

        targets: list[GrantTarget] = []
        for row in rows:
            targets.append(
                GrantTarget(
                    kind=GrantKind(row[GrantsColumn.TGT_KIND.value]),
                    id=UUID(str(row[GrantsColumn.TGT_KIND_ID.value])),
                )
            )

        return targets

    async def for_subject_all(self, subject: Subject) -> SubjectConnections:
        """Все соединения субъекта: разобранные строки плюс строки без типа.

        Строка типа без установленного пакета не теряется — она попадает в
        missing и показывается спискам с пометкой.
        """
        raw_rows = await self._subject_rows(subject, ConnectionFilter.none())

        rows: list[StoredConnection] = []
        missing: list[MissingTypeConnection] = []
        for row in raw_rows:
            try:
                rows.append(self._stored(row))
            except UnknownConnectionKindError as exc:
                missing.append(
                    MissingTypeConnection(
                        id=UUID(str(row[SubjectRowColumn.ID])),
                        name=row[SubjectRowColumn.NAME],
                        kind=exc.kind,
                    )
                )

        return SubjectConnections(rows=rows, missing=missing)

    async def for_subject(
        self, subject: Subject, kind: str
    ) -> Sequence[GrantedConnection]:
        """Соединения вида kind, выданные пользователю лично или любой его роли,
        с признаком дубля имени."""
        rows = await self._subject_rows(subject, ConnectionFilter.of_kind(kind))

        return list(self._granted_rows(rows))

    async def _subject_rows(
        self, subject: Subject, flt: ConnectionFilter
    ) -> list[dict[str, Any]]:
        """Строки SubjectGrantsQuery, прошедшие фильтр."""
        grants = SubjectGrantsQuery(subject, flt)
        query = self._query().add(grants.text(), **grants.params()).build()

        return await self._rows(query, "for subject")

    def _grant_params(self, connection_id: UUID, target: GrantTarget) -> dict[str, Any]:
        return {
            "src_kind": GrantKind.CONNECTIONS.value,
            "src_kind_id": connection_id,
            "tgt_kind": target.kind.value,
            "tgt_kind_id": target.id,
        }

    def _stored_rows(self, rows: Sequence[Mapping[str, Any]]) -> list[StoredConnection]:
        """Строки списком: запись типа без установленного пакета пропускается.

        Пропуск не молчалив: warning с именем строки и kind; точечный get такой
        строки падает UnknownConnectionKindError — внятной ошибкой использования.
        """
        stored: list[StoredConnection] = []
        for row in rows:
            try:
                stored.append(self._stored(row))
            except UnknownConnectionKindError as exc:
                logger.warning(
                    "connections: row #%s %r skipped: %s",
                    row["id"],
                    row["name"],
                    exc,
                )

        return stored

    def _granted_rows(
        self, rows: Sequence[Mapping[str, Any]]
    ) -> Iterator[GrantedConnection]:
        """Строки субъекта с признаком дубля; строка без типа пропускается
        так же, как в _stored_rows."""
        for row in rows:
            try:
                stored = self._stored(row)
            except UnknownConnectionKindError as exc:
                logger.warning(
                    "connections: row #%s %r skipped: %s",
                    row[SubjectRowColumn.ID],
                    row[SubjectRowColumn.NAME],
                    exc,
                )
                continue

            copies = int(row[SubjectRowColumn.COPIES])

            yield GrantedConnection(row=stored, ambiguous=copies > 1)

    def _stored(self, row: Mapping[str, Any]) -> StoredConnection:
        try:
            connection = self._types.parse(self._cipher.decrypt(row["data"]))
        except UnknownConnectionKindError:
            raise
        except ConnectionTypesError as exc:
            # from None: в разобранной строке лежит пароль, причину печатает
            # текст самой ошибки, мимо FailureText
            msg = (
                f"connections: row #{row['id']} {row['name']!r} is not a valid "
                f"connection connection: {exc}"
            )
            raise ConnectionStoreError(msg) from None

        return StoredConnection(
            id=UUID(str(row["id"])), name=row["name"], connection=connection
        )
