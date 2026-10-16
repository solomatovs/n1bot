"""Таблица users приложения: единственный владелец строки пользователя.

Одна схема и один набор запросов для chainlit и studio: строка входа, чтение по
логину и id, выбранный профиль studio в jsonb meta.

Ошибки:
DataUnavailableError — postgres недоступен или ответил не тем.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from boba.identity.api import (
    AuthenticatedUser,
    StoredUser,
    UserRows,
    UsersColumn,
    UserSettingsStore,
    UsersUpsert,
)
from boba.identity.session import Login, UserMetadataField
from boba.identity.signin import SignedIn
from boba.runtime.table import PgTable

__all__ = ["UsersTable"]


class UsersTable(PgTable, UserRows, UserSettingsStore, UsersUpsert):
    """users приложения: DDL, строки входа и настройки пользователя."""

    def _stored(self, row: Mapping[str, Any]) -> StoredUser:
        meta = row[UsersColumn.META.value]
        if meta is None:
            meta = {}

        return StoredUser(
            id=row[UsersColumn.ID.value],
            identifier=row[UsersColumn.IDENTIFIER.value],
            created_at=row[UsersColumn.CREATED_AT.value],
            meta=meta,
        )

    async def stored(self, identifier: Login) -> StoredUser | None:
        query = (
            self._query()
            .add(
                """
                select id, identifier, created_at, meta
                from {schema}.users
                where identifier = %(identifier)s
                limit 1
                """,
                identifier=identifier,
            )
            .build()
        )
        row = await self._row(query, "get_user")
        if row is None:
            return None

        return self._stored(row)

    async def stored_by_id(self, user_id: UUID) -> StoredUser | None:
        query = (
            self._query()
            .add(
                """
                select id, identifier, created_at, meta
                from {schema}.users
                where id = %(user_id)s
                limit 1
                """,
                user_id=user_id,
            )
            .build()
        )
        row = await self._row(query, "get_user_by_id")
        if row is None:
            return None

        return self._stored(row)

    async def get_user(self, identifier: Login) -> AuthenticatedUser | None:
        stored = await self.stored(identifier)
        if stored is None:
            return None

        return stored.authenticated()

    async def upsert(self, identifier: Login, meta: Mapping[str, Any]) -> StoredUser:
        """Новая строка либо metadata поверх прежней; что писать — решает вызывающий."""
        query = (
            self._query()
            .add(
                """
                insert into {schema}.users (
                    identifier,
                    created_at,
                    meta
                )
                values (
                    %(identifier)s,
                    %(created_at)s,
                    %(meta)s
                )
                on conflict (identifier) do update set
                    meta = coalesce({schema}.users.meta, '{{}}'::jsonb) || excluded.meta
                returning
                    id, identifier, created_at, meta
                """,
                identifier=identifier,
                created_at=datetime.now(UTC),
                meta=Jsonb(dict(meta)),
            )
            .build()
        )
        row = self._returning(
            await self._row(query, "ensure_user"), f"upsert of user {identifier!r}"
        )

        return self._stored(row)

    async def ensure_user(self, signed: SignedIn) -> AuthenticatedUser:
        stored = await self.upsert(
            signed.identifier, signed.sign_in.persistable().render()
        )

        return stored.authenticated()

    async def set_studio_profile(self, user_id: UUID, profile: str) -> None:
        query = (
            self._query()
            .add(
                """
                update {schema}.users
                set
                    meta = coalesce(meta, '{{}}'::jsonb)
                        || jsonb_build_object(%(key)s::text, %(profile)s::text)
                where
                    id = %(user_id)s
                """,
                key=UserMetadataField.STUDIO_PROFILE,
                profile=profile,
                user_id=user_id,
            )
            .build()
        )

        await self._execute(query, "set_studio_profile")
