"""Чтение опубликованных endpoint'ов MCP и их ролевых grants из PostgreSQL.

Ошибки:
EndpointStoreError — каталог недоступен или строка не проходит валидацию;
    запуск или reload отказывает, сохраняя причину ошибки.
"""

from collections.abc import Sequence
from typing import ClassVar

from boba.db.postgres import PostgresTable
from boba.mcp_server.catalog import McpEndpoint


class EndpointStoreError(Exception):
    """Снимок каталога не получен; пустой каталог этим отказом не подменяется."""


class McpEndpointStore(PostgresTable):
    """Репозиторий MCP: возвращает модели для сборки, не принимает решений о входе."""

    LABEL: ClassVar[str] = "mcp_endpoints"

    def _failure(self, action: str, exc: Exception) -> EndpointStoreError:
        return EndpointStoreError(self._detail(action, exc))

    async def endpoint_list(self) -> Sequence[McpEndpoint]:
        query = (
            self._query()
            .add("""
            select
                e.id,
                e.prefix_url,
                e.title,
                e.description,
                e.instructions,
                e.enabled,
                e.tools,
                e.overrides,
                e.prompts,
                coalesce(
                    array_agg(distinct r.role) filter (where r.role is not null),
                    '{{}}'
                ) as roles
            from
                {schema}.mcp_endpoints e
                left join {schema}.grants g on
                        g.src_kind = 'mcp_endpoints'
                    and g.src_kind_id = e.id
                    and g.tgt_kind = 'roles'
                left join {schema}.roles r on
                        g.tgt_kind_id = r.id
            where
                e.enabled
            group by
                e.id
            order by
                e.prefix_url
        """)
            .build()
        )
        rows = await self._rows(
            query, "reading enabled endpoint definitions and grants"
        )

        return tuple(self._parse(McpEndpoint, row) for row in rows)
