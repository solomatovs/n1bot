"""Роли тестовых стендов: явные данные администратора, отдельно от входа."""

from collections.abc import Iterable
from uuid import UUID

from boba.db.postgres import PostgresTable
from boba.identity.api import RoleCatalog
from boba.identity.principal import UserIdentity


class MemoryRoles(RoleCatalog):
    def __init__(self, names: Iterable[str]) -> None:
        self.names = frozenset(names)

    async def resolve_roles(
        self, identity: UserIdentity, candidates: Iterable[str]
    ) -> frozenset[str]:
        return self.names.intersection(candidates)


class RoleSeed:
    def __init__(self, table: PostgresTable) -> None:
        self._table = table

    async def add(self, names: Iterable[str]) -> None:
        query = (
            self._table._query()
            .add(
                """
            insert into {schema}.roles (role)
            select unnest(%(roles)s::text[]) on conflict (role) do nothing
            """,
                roles=list(names),
            )
            .build()
        )
        await self._table._execute(query, "seed test roles")

    async def user(
        self, user_id: UUID, identity: UserIdentity, roles: Iterable[str]
    ) -> None:
        async with self._table._transaction("seed test user") as cur:
            query = (
                self._table._query()
                .add(
                    """
                insert into {schema}.users (
                    id,
                    realm,
                    login
                )
                values (
                    %(id)s,
                    %(realm)s,
                    %(login)s
                )
                on conflict (id)
                do nothing
                """,
                    id=user_id,
                    realm=identity.realm,
                    login=identity.login,
                )
                .build()
            )
            await cur.execute(query.text, query.params)
            query = (
                self._table._query()
                .add(
                    """
                insert into {schema}.user_roles (user_id, role_id)
                select %(id)s, id from {schema}.roles
                where role = any(%(roles)s::text[]) on conflict do nothing
                """,
                    id=user_id,
                    roles=list(roles),
                )
                .build()
            )
            await cur.execute(query.text, query.params)
