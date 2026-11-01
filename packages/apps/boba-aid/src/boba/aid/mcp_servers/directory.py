"""Операции приложения MCP-серверов: список, карточка, правка личных
подключений и переподключение. Единственное место логики приложения;
обработчики действий только разбирают запрос и вызывают отсюда.

Приложение показывает серверы, выданные зрителю лично или через его роли
(ServerSource), в порядке имён; сервер chat (ChatTools.NAME) собственным
сервером зрителя не бывает и в списке не появляется. Состояние каждого
сервера запрашивается отдельно: ошибка одного сервера не мешает другим.

Ошибки:
McpAppError — операцию не исполнить: имя не выдано зрителю, сервер
    недоступен или состояние не прочитано; наружу уходит только она, всё
    прочее упаковывается в неё с `from exc`.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Literal

from mcp.types import Tool as MtTool
from pydantic import BaseModel, ConfigDict, Field

from boba.aid.mcp_servers import McpAppError, McpOrigin
from boba.aid.mcp_servers.config import McpUserConfig
from boba.aid.mcp_servers.form import (
    McpForms,
    McpServerForm,
    McpServerFormView,
    ServerName,
)
from boba.aid.mcp_servers.sources import McpViewer, ServerRecord, ServerSource
from boba.aid.mcp_servers.store import StoredServer
from boba.mcp_client.client import (
    HttpAddress,
    McpAuthRequired,
    McpCaller,
    McpClientError,
    McpServers,
    McpUnansweredError,
)

__all__ = [
    "McpAppError",
    "McpDirectory",
    "McpOrigin",
    "McpServerDetail",
    "McpServerList",
    "McpServerRequest",
    "McpServerRow",
    "McpServerStatus",
    "McpState",
    "McpToolRow",
    "McpViewer",
]


class McpState(StrEnum):
    """Состояние подключения; сохранение возвращает pending без подключения."""

    PENDING = "pending"
    CONNECTED = "connected"
    FAILED = "failed"
    NEEDS_AUTH = "needs_auth"
    DISABLED = "disabled"


class McpServerRequest(BaseModel):
    """Payload действий приложения с одним сервером: его имя."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: ServerName


class McpServerRow(BaseModel):
    """Строка списка приложения: данные без подключения, отдаются сразу."""

    model_config = ConfigDict(frozen=True)

    name: ServerName
    origin: McpOrigin
    editable: bool
    transport: Literal["streamable-http", "sse", "stdio"]
    address: str
    enabled: bool
    auth: str
    connect_timeout_sec: float
    call_timeout_sec: float
    headers: Sequence[str] = ()


class McpServerStatus(BaseModel):
    """Состояние одного сервера приложения."""

    model_config = ConfigDict(frozen=True)

    name: ServerName
    state: McpState
    issue: str = ""
    tools: int = 0


class McpToolRow(BaseModel):
    """Строка карточки инструментов сервера."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    title: str = ""
    description: str = ""
    read_only: bool = False
    destructive: bool = False


class McpServerDetail(BaseModel):
    """Карточка сервера приложения: строка списка, состояние, инструкции
    и инструменты."""

    model_config = ConfigDict(frozen=True)

    row: McpServerRow
    state: McpState
    issue: str = ""
    tools: Sequence[McpToolRow] = ()
    instructions: str = ""


class McpServerList(BaseModel):
    """Ответ mcp_list: серверы зрителя в порядке имён."""

    model_config = ConfigDict(frozen=True)

    servers: Sequence[McpServerRow]


class McpDirectory:
    """Операции приложения над клиентом MCP и источником серверов.

    Создаётся провайдером процесса. Набор серверов зрителя берёт у
    ServerSource, подключения — у McpServers: состояние и карточка
    спрашивают порт сервера, переподключение закрывает и забывает его.
    """

    def __init__(
        self,
        source: ServerSource,
        servers: McpServers,
        defaults: McpUserConfig,
    ) -> None:
        self._source = source
        self._servers = servers
        self._forms = McpForms(defaults)

    async def listed(self, viewer: McpViewer) -> McpServerList:
        """Серверы зрителя в порядке имён, без chat."""
        records = await self._source.visible(viewer)

        rows: list[McpServerRow] = []
        for record in records:
            rows.append(self._row(record))

        return McpServerList(servers=rows)

    async def status(self, viewer: McpViewer, name: ServerName) -> McpServerStatus:
        """Состояние сервера name; имя не из набора зрителя — отказ."""
        record = await self._source.record(viewer, name)

        return await self._status(viewer.caller(), record)

    async def detail(self, viewer: McpViewer, name: ServerName) -> McpServerDetail:
        """Карточка сервера name с инструментами; имя не из набора зрителя —
        отказ."""
        record = await self._source.record(viewer, name)
        row = self._row(record)
        status = await self._status(viewer.caller(), record)
        if status.state is not McpState.CONNECTED:
            return McpServerDetail(
                row=row, state=status.state, issue=status.issue, tools=()
            )

        try:
            port = await self._servers.port(record.name, viewer.caller())
            description = await port.described(McpServers.OWN)
            listed = await port.tools(McpServers.OWN)
        except McpClientError as exc:
            msg = (
                f"Reading MCP server {name!r} details for "
                f"{viewer.login!r} failed: {exc}"
            )
            raise McpAppError(msg) from exc

        instructions = description.result.instructions
        if instructions is None:
            instructions = ""

        tools = map(self._tool_row, listed.tools)

        return McpServerDetail(
            row=row,
            state=status.state,
            tools=tuple(tools),
            instructions=instructions,
        )

    async def form(self, viewer: McpViewer, name: ServerName) -> McpServerFormView:
        record = await self._source.record(viewer, name)

        return self._forms.view(name, self._stored(record), record.editable)

    async def save(
        self, viewer: McpViewer, form: McpServerForm, current: str | None = None
    ) -> McpServerDetail:
        if current is None:
            server = self._forms.server(form)
            record = await self._source.add(viewer, form.name, server)
        else:
            previous = await self._own(viewer, current)
            server = self._forms.server(form, self._stored(previous))
            record = await self._source.replace(viewer, current, form.name, server)
            await self._reconnected(viewer.caller(), current)

        return self._saved(record)

    async def set_enabled(
        self, viewer: McpViewer, name: ServerName, enabled: bool
    ) -> McpServerDetail:
        await self._own(viewer, name)
        await self._reconnected(viewer.caller(), name)
        record = await self._source.set_enabled(viewer, name, enabled)

        return self._saved(record)

    async def remove(self, viewer: McpViewer, name: ServerName) -> McpServerList:
        await self._own(viewer, name)
        await self._reconnected(viewer.caller(), name)
        await self._source.remove(viewer, name)

        return await self.listed(viewer)

    async def _own(self, viewer: McpViewer, name: str) -> ServerRecord:
        record = await self._source.record(viewer, name)
        if not record.editable:
            raise McpAppError(
                f"Changing MCP server {name!r}: expected a server granted "
                f"to {viewer.login!r} personally"
            )

        return record

    @staticmethod
    def _stored(record: ServerRecord) -> StoredServer:
        endpoint = record.config.endpoint
        if not isinstance(endpoint, HttpAddress):
            raise McpAppError(
                f"Editing MCP server {record.name!r}: expected an HTTP or SSE endpoint"
            )

        return StoredServer(endpoint=endpoint, limits=record.config.limits)

    def _saved(self, record: ServerRecord) -> McpServerDetail:
        state = McpState.PENDING
        if not record.enabled:
            state = McpState.DISABLED

        return McpServerDetail(row=self._row(record), state=state)

    async def reconnect(self, viewer: McpViewer, name: ServerName) -> McpServerStatus:
        """Переподключение сервера name: порт закрывается и забывается,
        состояние берётся заново. Имя не из набора зрителя — отказ."""
        record = await self._source.record(viewer, name)
        if record.enabled:
            await self._reconnected(
                viewer.caller(), record.name, all_callers=record.editable
            )

        return await self._status(viewer.caller(), record)

    async def _status(self, caller: McpCaller, record: ServerRecord) -> McpServerStatus:
        """Состояние сервера подключением и списком инструментов; выключенный
        сервер не подключается."""
        if not record.enabled:
            return McpServerStatus(name=record.name, state=McpState.DISABLED)

        try:
            port = await self._servers.port(record.name, caller)
            listed = await port.tools(McpServers.OWN)
        except McpAuthRequired as exc:
            return McpServerStatus(
                name=record.name, state=McpState.NEEDS_AUTH, issue=exc.brief
            )
        except McpUnansweredError as exc:
            return McpServerStatus(
                name=record.name, state=McpState.FAILED, issue=exc.brief
            )
        except McpClientError as exc:
            return McpServerStatus(
                name=record.name, state=McpState.FAILED, issue=str(exc)
            )

        return McpServerStatus(
            name=record.name, state=McpState.CONNECTED, tools=len(listed.tools)
        )

    async def _reconnected(
        self, caller: McpCaller, name: str, all_callers: bool = True
    ) -> None:
        """Порт сервера закрыт и забыт; отказ клиента — McpAppError."""
        try:
            if all_callers:
                await self._servers.invalidate(name)
            else:
                await self._servers.reconnect(name, caller)
        except McpClientError as exc:
            msg = (
                f"the MCP servers app did not reconnect the server {name!r} of "
                f"{caller.login!r}: the client refused: {exc}"
            )
            raise McpAppError(msg) from exc

    @staticmethod
    def _row(record: ServerRecord) -> McpServerRow:
        """Строка списка по записи сервера: адрес без секретов."""
        endpoint = record.config.endpoint
        auth = "none"
        headers: Sequence[str] = ()
        if isinstance(endpoint, HttpAddress):
            auth = endpoint.auth.auth
            headers = tuple(endpoint.headers)

        return McpServerRow(
            name=record.name,
            origin=record.origin,
            editable=record.editable,
            transport=endpoint.transport,
            address=endpoint.label(),
            enabled=record.enabled,
            auth=auth,
            headers=headers,
            connect_timeout_sec=record.config.limits.connect_timeout_sec,
            call_timeout_sec=record.config.limits.call_timeout_sec,
        )

    @staticmethod
    def _tool_row(tool: MtTool) -> McpToolRow:
        """Строка карточки инструмента из его объявления."""
        title = tool.title
        if not title:
            title = ""

        description = tool.description
        if not description:
            description = ""

        annotations = tool.annotations
        read_only = False
        destructive = False
        if annotations:
            read_only = bool(annotations.read_only_hint)
            destructive = bool(annotations.destructive_hint)

        return McpToolRow(
            name=tool.name,
            title=title,
            description=description,
            read_only=read_only,
            destructive=destructive,
        )
