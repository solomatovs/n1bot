"""Каталоги connections из API выданных MCP-серверов для команды чата.

Ошибки:
McpAppError — список серверов недоступен. Отказы отдельных серверов явно
    показаны рядом с их именами, доступные каталоги продолжают отображаться.
"""

import json

import mcp_types as mt
from mcp.shared.exceptions import MCPError
from pydantic import TypeAdapter, ValidationError

from boba.aid.mcp_servers.sources import McpViewer, ServerSource
from boba.connections.catalog import CatalogEntry, ConnectionTool
from boba.identity.context import Subject
from boba.mcp_client.client import McpCaller, McpClientError, McpServers
from boba.toolkit.mcp import McpResults
from boba.toolkit.result import MarkdownTable


class McpConnectionCatalogs:
    """Чат читает connection_list от имени пользователя; grants решает сам MCP."""

    def __init__(self, servers: McpServers, source: ServerSource) -> None:
        self._servers = servers
        self._source = source
        self._results = McpResults()
        self._decoder = json.JSONDecoder()
        self._entries = TypeAdapter(tuple[CatalogEntry, ...])

    async def markdown(self, subject: Subject) -> str:
        viewer = McpViewer(
            user_id=subject.user_id, login=subject.login, roles=subject.roles
        )
        caller = McpCaller(login=subject.login, roles=subject.roles)
        sections: list[str] = []
        for record in await self._source.enabled(viewer):
            try:
                shown = await self._catalog(record.name, caller)
            except (McpClientError, MCPError, ValueError) as exc:
                shown = f"Connection catalog could not be read: {exc}"

            if not shown:
                continue

            sections.append(f"**{record.name}**\n\n{shown}")

        if not sections:
            return "No connection catalog is offered by your MCP servers."

        return "\n\n".join(sections)

    async def _catalog(self, server: str, caller: McpCaller) -> str:
        port = await self._servers.port(server, caller)
        listed = await port.tools(McpServers.OWN)
        card = None
        for offered in listed.tools:
            if offered.name == ConnectionTool.LIST:
                card = offered
                break

        if card is None:
            return ""

        params = mt.CallToolRequestParams(name=ConnectionTool.LIST, arguments={})
        reply = await port.call(McpServers.OWN, params, card, None, None, None)
        result = self._results.result(ConnectionTool.LIST, "connections-menu", reply)
        if not result.ok:
            return result.llm_view()

        text = result.llm_view()
        try:
            raw, end = self._decoder.raw_decode(text)
            entries = self._entries.validate_python(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            msg = f"MCP server {server!r}: connection_list did not return catalog rows"
            raise ValueError(msg) from exc

        if not entries:
            return "No connections are granted to you."

        rows = [entry.model_dump() for entry in entries]
        table = MarkdownTable.render(rows)
        note = text[end:].strip()
        if note:
            return f"{table}\n\n{note}"

        return table
