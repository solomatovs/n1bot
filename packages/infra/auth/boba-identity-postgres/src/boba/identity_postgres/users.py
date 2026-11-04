"""Таблица users приложения: единственный владелец строки пользователя.

Личность хранится явными колонками, членство — в user_roles. Данные сессии
и произвольные metadata в users не сохраняются.

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

from boba.db.postgres import Cursor, PostgresTable
from boba.identity.api import (
    AuthenticatedUser,
    PersistedUsers,
    RoleCatalog,
    StoredUser,
    UserIdentityChange,
    UserIdentityMigration,
    UserIdentitySnapshot,
    UserRows,
    UsersColumn,
    UsersUpsert,
)
from boba.identity.errors import ExternalServiceError
from boba.identity.principal import UserIdentity
from boba.identity.session import Login
from boba.identity.signin import SignedIn

__all__ = ["UsersTable"]


class UsersTable(PostgresTable, UserRows, PersistedUsers, UsersUpsert, RoleCatalog):
    """Общие users, роли и членство: вход приложения и сохранённая личность."""

    LABEL: ClassVar[str] = "identity"

    def _failure(self, action: str, exc: Exception) -> Exception:
        return ExternalServiceError("identity", self._detail(action, exc))

    SELECT = """
        select u.id, u.identifier, u.created_at, u.realm, u.login,
            array(select r.role from {schema}.user_roles as m
                join {schema}.roles as r on r.id = m.role_id
                where m.user_id = u.id order by r.role) as roles
        from {schema}.users as u
    """

    def _stored(self, row: Mapping[str, Any]) -> StoredUser:
        identity = None
        if row[UsersColumn.REALM] is not None:
            identity = UserIdentity(
                realm=row[UsersColumn.REALM], login=row[UsersColumn.LOGIN]
            )
        return StoredUser(
            id=row[UsersColumn.ID],
            identifier=row[UsersColumn.IDENTIFIER],
            created_at=row[UsersColumn.CREATED_AT],
            identity=identity,
            roles=frozenset(row[UsersColumn.ROLES]),
        )

    async def stored(self, identifier: Login) -> StoredUser | None:
        query = (
            self._query()
            .add(
                self.SELECT + "where u.identifier = %(identifier)s limit 1",
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
                self.SELECT + "where u.id = %(user_id)s limit 1",
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

    async def upsert(
        self,
        identifier: Login,
        identity: UserIdentity | None = None,
        roles: frozenset[str] | None = None,
    ) -> StoredUser:
        if identity is not None and not identity.matches(identifier):
            raise ValueError("user identifier must match the confirmed identity")

        async def persist(cur: Cursor) -> StoredUser:
            user_id = await self._upsert(cur, identifier, identity)
            if roles is not None:
                await self._sync_roles(cur, user_id, roles)
            query = (
                self._query()
                .add(
                    self.SELECT + "where u.identifier = %(identifier)s",
                    identifier=identifier,
                )
                .build()
            )
            await cur.execute(query.text, query.params)
            row = await cur.fetchone()
            if row is None:
                raise RuntimeError("inserted user is missing")
            return self._stored(row)

        return await self._transacted("ensure user and role membership", persist)

    async def _upsert(
        self, cur: Cursor, identifier: Login, identity: UserIdentity | None
    ) -> UUID:
        query = (
            self._query()
            .add(
                """
            insert into {schema}.users (identifier, realm, login)
            select %(identifier)s, %(realm)s, %(login)s
            where not exists (
                select 1 from {schema}.users
                where identifier = %(previous)s and identifier != %(identifier)s
            )
            and not exists (
                select 1 from {schema}.users where identifier = %(identifier)s
                and (realm is distinct from %(realm)s
                    or login is distinct from %(login)s)
            )
            on conflict (identifier) do update set identifier = excluded.identifier
            where {schema}.users.realm is not distinct from excluded.realm
                and {schema}.users.login is not distinct from excluded.login
            returning id
            """,
                identifier=identifier,
                realm=identity.realm if identity is not None else None,
                login=identity.login if identity is not None else None,
                previous=identity.login if identity is not None else "",
            )
            .build()
        )
        await cur.execute(query.text, query.params)
        row = await cur.fetchone()
        if row is None:
            msg = (
                f"sign-in of {identifier!r}: legacy or conflicting user requires "
                "an explicit identity mapping before login; existing history "
                "and personal grants have not been reassigned"
            )
            raise ExternalServiceError("identity", msg)
        return row[UsersColumn.ID]

    async def resolve_roles(
        self, identity: UserIdentity, candidates: Iterable[str]
    ) -> frozenset[str]:
        query = (
            self._query()
            .add(
                """
            select r.role from {schema}.roles as r
            where r.role = any(%(roles)s::text[])
                or exists (
                    select 1 from {schema}.user_roles as m
                    join {schema}.users as u on u.id = m.user_id
                    where m.role_id = r.id and not m.managed
                        and u.identifier = %(identifier)s
                        and u.realm = %(realm)s and u.login = %(login)s
                )
            """,
                roles=list(candidates),
                identifier=identity.identifier,
                realm=identity.realm,
                login=identity.login,
            )
            .build()
        )
        rows = await self._rows(query, "resolve roles")
        return frozenset(row["role"] for row in rows)

    async def _sync_roles(
        self, cur: Cursor, user_id: UUID, roles: frozenset[str]
    ) -> None:
        queries = (
            self._query()
            .add(
                """
                delete from {schema}.user_roles as m
                using {schema}.roles as r
                where m.user_id = %(user_id)s and m.role_id = r.id and m.managed
                    and not (r.role = any(%(roles)s::text[]))
                """,
                user_id=user_id,
                roles=list(roles),
            )
            .build(),
            self._query()
            .add(
                """
                insert into {schema}.user_roles (user_id, role_id, managed)
                select %(user_id)s, id, true from {schema}.roles
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
        identity = signed.sign_in.identity_for(signed.identifier)
        stored = await self.upsert(signed.identifier, identity, signed.sign_in.roles)
        return AuthenticatedUser(
            id=stored.id,
            identifier=stored.identifier,
            created_at=stored.created_at,
            sign_in=signed.sign_in.model_copy(update={"roles": stored.roles}),
        )

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

                    roles = frozenset()
                    created_at = datetime.now(UTC)
                else:
                    allowed = (assignment.previous_identifier, identity.identifier)
                    if before.identifier not in allowed:
                        msg = (
                            f"identity migration user {assignment.user_id}: "
                            f"expected identifier {allowed}, got {before.identifier!r}"
                        )
                        raise ValueError(msg)

                    roles = before.roles
                    created_at = before.created_at

                after = StoredUser(
                    id=assignment.user_id,
                    identifier=identity.identifier,
                    created_at=created_at,
                    identity=identity,
                    roles=roles,
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
                        insert into {schema}.users
                            (id, identifier, created_at, realm, login)
                        values
                            (%(user_id)s, %(identifier)s, %(created_at)s,
                             %(realm)s, %(login)s)
                        on conflict (id) do update set
                            identifier = excluded.identifier,
                            created_at = excluded.created_at,
                            realm = excluded.realm,
                            login = excluded.login
                        """,
                            user_id=target.id,
                            identifier=target.identifier,
                            created_at=target.created_at,
                            realm=target.identity.realm
                            if target.identity is not None
                            else None,
                            login=target.identity.login
                            if target.identity is not None
                            else None,
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
        query = self._query().add(self.SELECT).build()
        await cur.execute(query.text, query.params)
        rows = await cur.fetchall()

        users: dict[UUID, StoredUser] = {}
        for row in rows:
            user = self._stored(row)
            users[user.id] = user

        return users
