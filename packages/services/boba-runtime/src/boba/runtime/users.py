"""Таблица users приложения: единственный владелец строки пользователя.

Одна схема и один набор запросов для chainlit и studio: строка входа, чтение по
логину и id, настройки LLM и выбранный профиль studio в jsonb meta.

Ошибки:
DataUnavailableError — postgres недоступен или ответил не тем.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg import sql
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from boba.chat.threads import DataUnavailableError
from boba.connections.postgres import PostgresConfig
from boba.db.postgres import AsyncPostgresPool
from boba.identity.api import (
    AuthenticatedUser,
    StoredUser,
    UserRows,
    UserSettingsStore,
    UsersUpsert,
)
from boba.identity.session import UserMetadataField
from boba.identity.signin import SignedIn
from boba.runtime.tables import ChatTable, UsersColumn

__all__ = ["UsersTable"]


class UsersTable(UserRows, UserSettingsStore, UsersUpsert):
    """users приложения: DDL, строки входа и настройки пользователя."""

    def __init__(
        self,
        postgres: PostgresConfig,
        db_schema: str,
        pool: AsyncPostgresPool | None = None,
    ) -> None:
        self._postgres = postgres
        self._schema = db_schema
        self._pool_ref = pool

    async def _pool(self) -> AsyncPostgresPool:
        if self._pool_ref is None:
            self._pool_ref = await AsyncPostgresPool.get(self._postgres)

        return self._pool_ref

    def _users(self) -> sql.Identifier:
        return ChatTable.USERS.under(self._schema)

    def _row_columns(self) -> sql.Composed:
        return sql.SQL(", ").join(
            [
                UsersColumn.ID.ident(),
                UsersColumn.IDENTIFIER.ident(),
                UsersColumn.CREATED_AT.ident(),
                UsersColumn.META.ident(),
            ]
        )

    @staticmethod
    def _stored(row: tuple[Any, ...]) -> StoredUser:
        meta = row[3]
        if meta is None:
            meta = {}

        return StoredUser(id=row[0], identifier=row[1], created_at=row[2], meta=meta)

    async def setup(self) -> None:
        """Создаёт схему и таблицу users; повтор безвреден."""
        ddl = (
            sql.SQL("create schema if not exists {}").format(
                sql.Identifier(self._schema)
            ),
            sql.SQL(
                """
                create table if not exists {users} (
                    {id}         uuid primary key default gen_random_uuid(),
                    {identifier} text not null unique,
                    {created_at} timestamptz not null default now(),
                    {meta}       jsonb not null default '{{}}'::jsonb
                )
                """
            ).format(
                users=self._users(),
                id=UsersColumn.ID.ident(),
                identifier=UsersColumn.IDENTIFIER.ident(),
                created_at=UsersColumn.CREATED_AT.ident(),
                meta=UsersColumn.META.ident(),
            ),
            # регистр логина не заводит вторую личность: инвариант держит база
            sql.SQL(
                """
                create unique index if not exists idx_users_identifier_lower
                    on {users} (lower({identifier}))
                """
            ).format(users=self._users(), identifier=UsersColumn.IDENTIFIER.ident()),
        )
        try:
            pool = await self._pool()
            async with pool.connection() as conn, conn.transaction():
                for statement in ddl:
                    await conn.execute(statement)
        except Exception as exc:
            raise DataUnavailableError("users.setup", str(exc)) from exc

    async def stored(self, identifier: str) -> StoredUser | None:
        query = sql.SQL(
            "select {cols} from {users} where {identifier} = %(identifier)s limit 1"
        ).format(
            cols=self._row_columns(),
            users=self._users(),
            identifier=UsersColumn.IDENTIFIER.ident(),
        )

        return await self._one(query, {"identifier": identifier}, "get_user")

    async def stored_by_id(self, user_id: UUID) -> StoredUser | None:
        query = sql.SQL(
            "select {cols} from {users} where {id} = %(user_id)s limit 1"
        ).format(
            cols=self._row_columns(), users=self._users(), id=UsersColumn.ID.ident()
        )

        return await self._one(query, {"user_id": user_id}, "get_user_by_id")

    async def _one(
        self, query: sql.Composed, params: Mapping[str, Any], operation: str
    ) -> StoredUser | None:
        try:
            pool = await self._pool()
            async with (
                pool.connection() as conn,
                conn.cursor(row_factory=tuple_row) as cur,
            ):
                await cur.execute(query, params)
                row = await cur.fetchone()
        except Exception as exc:
            raise DataUnavailableError(operation, str(exc)) from exc

        if row is None:
            return None

        return self._stored(row)

    async def get_user(self, identifier: str) -> AuthenticatedUser | None:
        stored = await self.stored(identifier)
        if stored is None:
            return None

        return stored.authenticated()

    async def upsert(self, identifier: str, meta: Mapping[str, Any]) -> StoredUser:
        """Новая строка либо metadata поверх прежней; билет SSO в строку не пишется."""
        metadata = dict(meta)
        metadata.pop(UserMetadataField.TICKET, None)
        query = sql.SQL(
            """
            insert into {users} (
                {identifier},
                {created_at},
                {meta}
            )
            values (
                %(identifier)s,
                %(created_at)s,
                %(meta)s
            )
            on conflict ({identifier}) do update set
                {meta} = coalesce({users}.{meta}, '{{}}'::jsonb) || excluded.{meta}
            returning
                {cols}
            """
        ).format(
            users=self._users(),
            cols=self._row_columns(),
            identifier=UsersColumn.IDENTIFIER.ident(),
            created_at=UsersColumn.CREATED_AT.ident(),
            meta=UsersColumn.META.ident(),
        )
        params = {
            "identifier": identifier,
            "created_at": datetime.now(UTC),
            "meta": Jsonb(metadata),
        }

        row = await self._one(query, params, "ensure_user")
        if row is None:
            raise DataUnavailableError("ensure_user", "users row was not returned")

        return row

    async def ensure_user(self, signed: SignedIn) -> AuthenticatedUser:
        stored = await self.upsert(signed.identifier, signed.metadata)

        return stored.authenticated()

    async def set_studio_profile(self, user_id: UUID, profile: str) -> None:
        query = sql.SQL(
            """
            update {users}
            set
                {meta} = coalesce({meta}, '{{}}'::jsonb)
                    || jsonb_build_object(%(key)s::text, %(profile)s::text)
            where
                {id} = %(user_id)s
            """
        ).format(
            users=self._users(),
            meta=UsersColumn.META.ident(),
            id=UsersColumn.ID.ident(),
        )
        params = {
            "key": UserMetadataField.STUDIO_PROFILE,
            "profile": profile,
            "user_id": user_id,
        }

        await self._execute(query, params, "set_studio_profile")

    async def set_llm_settings(
        self, user_id: UUID, profile: str, values: Mapping[str, Any]
    ) -> None:
        path = [UserMetadataField.LLM, profile]
        if values:
            query = sql.SQL(
                """
                update {users}
                set
                    {meta} = jsonb_set(
                        jsonb_set(
                            coalesce({meta}, '{{}}'::jsonb),
                            %(llm)s,
                            coalesce({meta} -> %(llm_key)s, '{{}}'::jsonb)
                        ),
                        %(path)s,
                        %(values)s
                    )
                where
                    {id} = %(user_id)s
                """
            ).format(
                users=self._users(),
                meta=UsersColumn.META.ident(),
                id=UsersColumn.ID.ident(),
            )
            params: dict[str, Any] = {
                "user_id": user_id,
                "llm": [UserMetadataField.LLM],
                "llm_key": UserMetadataField.LLM,
                "path": path,
                "values": Jsonb(dict(values)),
            }
        else:
            query = sql.SQL(
                """
                update {users}
                set
                    {meta} = coalesce({meta}, '{{}}'::jsonb) #- %(path)s
                where
                    {id} = %(user_id)s
                """
            ).format(
                users=self._users(),
                meta=UsersColumn.META.ident(),
                id=UsersColumn.ID.ident(),
            )
            params = {"user_id": user_id, "path": path}

        await self._execute(query, params, "set_llm_settings")

    async def _execute(
        self, query: sql.Composed, params: Mapping[str, Any], operation: str
    ) -> None:
        try:
            pool = await self._pool()
            async with pool.connection() as conn:
                await conn.execute(query, params)
        except Exception as exc:
            raise DataUnavailableError(operation, str(exc)) from exc
