"""Операции приложения MCP-серверов над клиентом MCP: список, состояние,
карточка и переподключение. Единственное место логики приложения;
обработчики действий только разбирают запрос и вызывают отсюда.

Приложение показывает серверы профиля зрителя (`[profiles.<имя>].mcp`) в
порядке профиля; сервер chat (ChatTools.NAME) — собственные инструменты
чата — в нём не показывается. Состояние каждого сервера запрашивается
отдельно: ошибка одного сервера не мешает другим.

Ошибки:
McpAppError — операцию не исполнить: имя не из набора зрителя, сервер
    недоступен или состояние не прочитано; наружу уходит только она, всё
    прочее упаковывается в неё с `from exc`.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated, Literal

from mcp.types import Tool as MtTool
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from boba.aid.agent.relay import ChatTools
from boba.chat.profiles import SelectedProfile
from boba.mcp_client.client import (
    McpAuthRequired,
    McpCaller,
    McpClientError,
    McpServerConfig,
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


class McpAppError(Exception):
    """Операцию приложения не исполнить; текст — для пользователя."""


class McpOrigin(StrEnum):
    """Откуда сервер в приложении зрителя."""

    PROFILE = "profile"
    USER = "user"


class McpState(StrEnum):
    """Состояние сервера в приложении; pending бывает только на фронте."""

    CONNECTED = "connected"
    FAILED = "failed"
    NEEDS_AUTH = "needs_auth"
    DISABLED = "disabled"


ServerName = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
"""Имя сервера приложения: секция [mcp.servers.<имя>] конфига."""


class McpViewer(BaseModel):
    """Кто смотрит приложение: логин, роли и профиль сессии."""

    model_config = ConfigDict(frozen=True)

    login: str = Field(min_length=1)
    roles: frozenset[str]
    profile: SelectedProfile

    def caller(self) -> McpCaller:
        """Вызывающий приложения для клиента MCP."""
        return McpCaller(login=self.login, roles=self.roles)


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
    """Ответ mcp_list: серверы зрителя в порядке профиля."""

    model_config = ConfigDict(frozen=True)

    servers: Sequence[McpServerRow]


class McpDirectory:
    """Операции приложения над клиентом MCP; создаётся провайдером на процесс."""

    def __init__(self, servers: McpServers) -> None:
        self._servers = servers

    async def listed(self, viewer: McpViewer) -> McpServerList:
        """Серверы профиля зрителя в порядке профиля, без chat."""
        rows: list[McpServerRow] = []
        for name in self._profile_names(viewer):
            config = self._named(viewer, name)
            rows.append(self._row(name, config))

        return McpServerList(servers=rows)

    async def status(self, viewer: McpViewer, name: ServerName) -> McpServerStatus:
        """Состояние сервера name; имя не из набора зрителя — отказ."""
        self._named(viewer, name)
        return await self._status(viewer.caller(), name)

    async def detail(self, viewer: McpViewer, name: ServerName) -> McpServerDetail:
        """Карточка сервера name с инструментами; имя не из набора зрителя —
        отказ."""
        config = self._named(viewer, name)
        row = self._row(name, config)
        status = await self._status(viewer.caller(), name)
        if status.state is not McpState.CONNECTED:
            return McpServerDetail(
                row=row, state=status.state, issue=status.issue, tools=()
            )

        port = await self._port(viewer.caller(), name)
        description = await port.described(McpServers.OWN)
        listed = await port.tools(McpServers.OWN)
        return McpServerDetail(
            row=row,
            state=status.state,
            tools=tuple(self._tool_row(tool) for tool in listed.tools),
            instructions=description.result.instructions or "",
        )

    async def reconnect(self, viewer: McpViewer, name: ServerName) -> McpServerStatus:
        """Переподключение сервера name: порт закрывается и забывается,
        состояние берётся заново. Имя не из набора зрителя — отказ."""
        self._named(viewer, name)
        try:
            await self._servers.reconnect(name, viewer.caller())
        except McpClientError as exc:
            msg = (
                f"the MCP servers app did not reconnect the server {name!r} of "
                f"{viewer.login!r}: the client refused: {exc}"
            )
            raise McpAppError(msg) from exc

        return await self._status(viewer.caller(), name)

    async def _status(self, caller: McpCaller, name: str) -> McpServerStatus:
        """Состояние сервера подключением и списком инструментов."""
        try:
            port = await self._port(caller, name)
            listed = await port.tools(McpServers.OWN)
        except McpAuthRequired as exc:
            return McpServerStatus(
                name=name, state=McpState.NEEDS_AUTH, issue=exc.brief
            )
        except McpUnansweredError as exc:
            return McpServerStatus(name=name, state=McpState.FAILED, issue=exc.brief)
        except McpClientError as exc:
            return McpServerStatus(name=name, state=McpState.FAILED, issue=str(exc))

        return McpServerStatus(
            name=name, state=McpState.CONNECTED, tools=len(listed.tools)
        )

    async def _port(self, caller: McpCaller, name: str):
        """Порт сервера от клиента; отказ клиента — McpAppError."""
        try:
            return await self._servers.port(name, caller)
        except McpClientError as exc:
            msg = (
                f"the MCP servers app did not reach the server {name!r} of "
                f"{caller.login!r}: the client refused: {exc}"
            )
            raise McpAppError(msg) from exc

    def _profile_names(self, viewer: McpViewer) -> list[str]:
        """Имена серверов профиля зрителя в порядке профиля, без chat."""
        return [name for name in viewer.profile.config.mcp if name != ChatTools.NAME]

    def _named(self, viewer: McpViewer, name: str) -> McpServerConfig:
        """Сервер name есть в наборе зрителя, иначе — отказ: чужой или
        админский сервер не назвать."""
        if name not in self._profile_names(viewer):
            names = ", ".join(self._profile_names(viewer)) or "none"
            msg = (
                f"the MCP servers app of {viewer.login!r} did not find the server "
                f"{name!r}: it is not among the profile servers {names}"
            )
            raise McpAppError(msg)

        return self._server_config(name)

    def _row(self, name: str, config: McpServerConfig) -> McpServerRow:
        """Строка списка по записи сервера в конфиге: адрес без секретов."""
        endpoint = config.endpoint
        return McpServerRow(
            name=name,
            origin=McpOrigin.PROFILE,
            editable=False,
            transport=endpoint.transport,
            address=endpoint.label(),
        )

    def _server_config(self, name: str) -> McpServerConfig:
        """Запись сервера name из конфига клиента."""
        try:
            return self._servers.config_of(name)
        except McpClientError as exc:
            msg = (
                f"the MCP servers app did not read the server {name!r}: "
                f"the client refused: {exc}"
            )
            raise McpAppError(msg) from exc

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
