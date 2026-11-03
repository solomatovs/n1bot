"""Таблица users приложения: единственный владелец строки пользователя.

Одна схема и один набор запросов: строка входа, чтение по логину и id, metadata
входа в jsonb meta.

Ошибки:
ExternalServiceError — postgres недоступен либо прежний логин ещё не сопоставлен
    новой личности.
AuthenticationError — в строке users нет подтверждённой личности.
ValueError — план миграции конфликтует с текущими строками users.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID

from psycopg.types.json import Jsonb

from boba.db.postgres import Cursor, PostgresTable
from boba.identity.api import (
    AuthenticatedUser,
    PersistedUsers,
    StoredUser,
    UserIdentityChange,
    UserIdentityMigration,
    UserIdentitySnapshot,
    UserRows,
    UsersColumn,
    UsersUpsert,
)
from boba.identity.errors import ExternalServiceError
from boba.identity.session import Login, UserMetadataField
from boba.identity.signin import SignedIn, SignInMetadata

__all__ = ["UsersTable"]


class UsersTable(PostgresTable, UserRows, PersistedUsers, UsersUpsert):
    """Общие users, роли и членство: вход приложения и сохранённая личность."""

    LABEL: ClassVar[str] = "identity"

    def _failure(self, action: str, exc: Exception) -> Exception:
        return ExternalServiceError("identity", self._detail(action, exc))

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
        sign_in = SignInMetadata.parse(meta)
        previous = ""
        checked_identity = None
        if sign_in.identity is not None:
            previous = sign_in.identity_for(identifier).login
            checked_identity = Jsonb(sign_in.identity.model_dump(mode="json"))

        query = (
            self._query()
            .add(
                """
                insert into {schema}.users (
                    identifier,
                    created_at,
                    meta
                )
                select
                    %(identifier)s,
                    %(created_at)s,
                    %(meta)s
                where not exists (
                    select 1 from {schema}.users
                    where identifier = %(previous)s
                      and identifier != %(identifier)s
                )
                and not exists (
                    select 1 from {schema}.users
                    where identifier = %(identifier)s
                      and meta -> %(identity_key)s
                          is distinct from %(checked_identity)s::jsonb
                )
                on conflict (identifier) do update set
                    meta = coalesce({schema}.users.meta, '{{}}'::jsonb) || excluded.meta
                returning
                    id, identifier, created_at, meta
                """,
                identifier=identifier,
                created_at=datetime.now(UTC),
                meta=Jsonb(dict(meta)),
                previous=previous,
                checked_identity=checked_identity,
                identity_key=UserMetadataField.IDENTITY,
            )
            .build()
        )

        async def persist(cur: Cursor) -> Mapping[str, Any] | None:
            await cur.execute(query.text, query.params)
            row = await cur.fetchone()
            if row is None:
                return None

            if UserMetadataField.ROLES in meta:
                await self._sync_roles(cur, row[UsersColumn.ID.value], sign_in.roles)

            return row

        row = await self._transacted("ensure user and role membership", persist)

        if row is None:
            msg = (
                f"sign-in of {identifier!r}: legacy or conflicting user requires "
                "an explicit identity mapping before login; existing history "
                "and personal grants have not been reassigned"
            )
            raise ExternalServiceError("identity", msg)

        return self._stored(row)

    async def sync_roles(self, names: Iterable[str]) -> None:
        """Регистрирует имена ролей, не меняя членство пользователей."""
        async with self._transaction("sync role names") as cur:
            await self._add_roles(cur, names)

    async def _add_roles(self, cur: Cursor, names: Iterable[str]) -> None:
        roles = sorted(names)

        query = (
            self._query()
            .add(
                """
            insert into {schema}.roles (role)
            select unnest(%(roles)s::text[])
            on conflict (role) do nothing
            """,
                roles=roles,
            )
            .build()
        )
        await cur.execute(query.text, query.params)

    async def _sync_roles(
        self, cur: Cursor, user_id: UUID, roles: frozenset[str]
    ) -> None:
        await self._add_roles(cur, roles)
        queries = (
            self._query()
            .add(
                """
                delete from {schema}.user_roles as m
                using {schema}.roles as r
                where m.user_id = %(user_id)s and m.role_id = r.id
                    and not (r.role = any(%(roles)s::text[]))
                """,
                user_id=user_id,
                roles=list(roles),
            )
            .build(),
            self._query()
            .add(
                """
                insert into {schema}.user_roles (user_id, role_id)
                select %(user_id)s, id from {schema}.roles
                where role = any(%(roles)s::text[])
                on conflict do nothing
                """,
                user_id=user_id,
                roles=list(roles),
            )
            .build(),
        )
        for query in queries:
            await cur.execute(query.text, query.params)

    async def ensure_user(self, signed: SignedIn) -> AuthenticatedUser:
        signed.sign_in.identity_for(signed.identifier)
        stored = await self.upsert(signed.identifier, signed.sign_in.stored())

        return stored.authenticated()

    async def identity_snapshot(
        self, migration: UserIdentityMigration
    ) -> UserIdentitySnapshot:
        """План миграции с исходными строками; данные users не меняются."""
        async with self._transaction("plan user identities") as cur:
            current = await self._locked_users(cur)
            by_identifier = {user.identifier: user for user in current.values()}
            changes: list[UserIdentityChange] = []
            for assignment in migration.users:
                identity = assignment.identity
                owner = by_identifier.get(identity.identifier)
                if owner is not None:
                    if owner.id != assignment.user_id:
                        msg = (
                            f"identity {identity.identifier!r} already belongs "
                            f"to user {owner.id}, not {assignment.user_id}"
                        )
                        raise ValueError(msg)

                before = current.get(assignment.user_id)
                if before is None:
                    if not migration.seed_missing:
                        msg = (
                            f"identity migration user {assignment.user_id} "
                            "does not exist"
                        )
                        raise ValueError(msg)

                    before_meta: Mapping[str, object] = {}
                    created_at = datetime.now(UTC)
                else:
                    allowed = (assignment.previous_identifier, identity.identifier)
                    if before.identifier not in allowed:
                        msg = (
                            f"identity migration user {assignment.user_id}: "
                            f"expected identifier {allowed}, got {before.identifier!r}"
                        )
                        raise ValueError(msg)

                    before_meta = before.meta
                    created_at = before.created_at

                meta = dict(before_meta)
                meta[UserMetadataField.IDENTITY] = identity.model_dump(mode="json")
                after = StoredUser(
                    id=assignment.user_id,
                    identifier=identity.identifier,
                    created_at=created_at,
                    meta=meta,
                )
                changes.append(UserIdentityChange(before=before, after=after))

            return UserIdentitySnapshot(schema_name=self.schema, changes=tuple(changes))

    async def apply_identity_snapshot(
        self, snapshot: UserIdentitySnapshot, *, rollback: bool = False
    ) -> None:
        """Атомарно применяет или откатывает план, если исходные строки совпадают."""
        if snapshot.schema_name != self.schema:
            msg = (
                f"identity migration targets schema {snapshot.schema_name!r}, "
                f"this users store owns {self.schema!r}"
            )
            raise ValueError(msg)

        async with self._transaction("apply user identities") as cur:
            current = await self._locked_users(cur)
            for change in snapshot.changes:
                expected = change.before
                target: StoredUser | None = change.after
                if rollback:
                    expected = change.after
                    target = change.before

                stored = current.get(change.after.id)
                if stored == target:
                    continue

                if stored != expected:
                    msg = (
                        f"identity migration user {change.after.id} changed after "
                        "the plan was prepared; create a new plan"
                    )
                    raise ValueError(msg)

            for change in snapshot.changes:
                target = change.after
                if rollback:
                    target = change.before

                if current.get(change.after.id) == target:
                    continue

                if target is None:
                    query = (
                        self._query()
                        .add(
                            "delete from {schema}.users where id = %(user_id)s",
                            user_id=change.after.id,
                        )
                        .build()
                    )
                else:
                    query = (
                        self._query()
                        .add(
                            """
                        insert into {schema}.users (id, identifier, created_at, meta)
                        values (%(user_id)s, %(identifier)s, %(created_at)s, %(meta)s)
                        on conflict (id) do update set
                            identifier = excluded.identifier,
                            created_at = excluded.created_at,
                            meta = excluded.meta
                        """,
                            user_id=target.id,
                            identifier=target.identifier,
                            created_at=target.created_at,
                            meta=Jsonb(dict(target.meta)),
                        )
                        .build()
                    )

                await cur.execute(query.text, query.params)

    async def _locked_users(self, cur: Cursor) -> dict[UUID, StoredUser]:
        lock = (
            self._query()
            .add("lock table {schema}.users in share row exclusive mode")
            .build()
        )
        await cur.execute(lock.text, lock.params)
        query = (
            self._query()
            .add("select id, identifier, created_at, meta from {schema}.users")
            .build()
        )
        await cur.execute(query.text, query.params)
        rows = await cur.fetchall()

        users: dict[UUID, StoredUser] = {}
        for row in rows:
            user = self._stored(row)
            users[user.id] = user

        return users
