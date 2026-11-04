"""Права ролей на инструменты: актуальный набор из PostgreSQL."""

from boba.access import RolePermissions
from boba.db.postgres import PostgresTable


class RoleToolsTable(PostgresTable, RolePermissions):
    async def tools_for_roles(self, roles: frozenset[str]) -> frozenset[str]:
        if not roles:
            return frozenset()
        query = (
            self._query()
            .add(
                """
            select distinct t.tool from {schema}.role_tools as t
            join {schema}.roles as r on r.id = t.role_id
            where (r.role = any(%(roles)s::text[]) or r.role = '*')
                and exists (
                    select 1 from {schema}.roles
                    where role = any(%(roles)s::text[])
                )
            """,
                roles=list(roles),
            )
            .build()
        )
        rows = await self._rows(query, "read role tool permissions")
        return frozenset(row["tool"] for row in rows)
