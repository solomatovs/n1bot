"""PostgreSQL catalog of MCP endpoints and their role grants."""

from __future__ import annotations

from typing import ClassVar

from boba.db.postgres import PostgresPool, PostgresTable
from boba.db.postgres.connection import PostgresConfig
from boba.mcp_server.server import McpEndpoint

__all__ = ["McpEndpointStore"]


class McpEndpointStore(PostgresTable):
    """Reads the MCP endpoint catalog from the application's own schema."""

    LABEL: ClassVar[str] = "mcp_endpoints"

    def __init__(
        self, postgres: PostgresConfig, schema: str, pool: PostgresPool
    ) -> None:
        super().__init__(postgres, schema, pool)

    async def list(self) -> dict[str, McpEndpoint]:
        query = (
            self._query()
            .add(
                """
                select
                    e.name,
                    e.description,
                    e.connections,
                    e.prompts,
                    coalesce(array_agg(distinct r.role) filter (where r.role is not null), '{}') as roles,
                    coalesce(array_agg(distinct t.tool) filter (where t.tool is not null), '{}') as tools
                from {schema}.mcp_endpoints as e
                left join {schema}.mcp_endpoint_tools as t on t.endpoint_id = e.id
                left join {schema}.grants as g
                    on g.src_kind = 'mcp_endpoints'
                    and g.src_kind_id = e.id
                    and g.tgt_kind = 'roles'
                left join {schema}.roles as r on r.id = g.tgt_kind_id
                group by e.id
                order by e.name
                """
            )
            .build()
        )
        rows = await self._rows(query, "read MCP endpoint catalog")
        return {
            row["name"]: McpEndpoint.model_validate(
                {
                    "description": row["description"],
                    "connections": row["connections"],
                    "prompts": row["prompts"],
                    "roles": row["roles"],
                    "tools": row["tools"],
                }
            )
            for row in rows
        }

