"""Источник MCP-серверов чата: набор серверов зрителя из таблицы mcp_servers
и конфиг сервера для клиента.

Единственная точка, через которую aid узнаёт, какие серверы видит
пользователь и по какой записи к ним подключаться. Набор строит выдача
grants: сервер, выданный лично, — свой (его владелец правит и удаляет),
выданный роли администратором — только на чтение. Записи читает
McpServerStore, пользователя по логину вызывающего — UsersTable.

Ошибки:
McpAppError — набор зрителя или запись сервера не прочитаны, сервер зрителю
    не выдан либо операция над чужим сервером запрещена.
McpClientError — конфиг сервера для клиента не прочитан.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from boba.aid.mcp_servers import McpAppError, McpOrigin
from boba.aid.mcp_servers.oauth import McpBrowserOAuth
from boba.aid.mcp_servers.store import (
    McpServerStore,
    McpServerStoreError,
    StoredMcpServer,
    StoredServer,
)
from boba.identity.api import UserRows
from boba.identity.errors import AuthenticationError
from boba.identity.session import Login
from boba.mcp_client.client import (
    BrowserOAuthAuth,
    McpCaller,
    McpClientError,
    McpServerConfig,
    ServerConfigs,
)

__all__ = [
    "McpOrigin",
    "McpViewer",
    "ServerRecord",
    "ServerSource",
    "StoredServers",
]


class McpViewer(BaseModel):
    """Кто смотрит серверы: строка users, логин и роли входа."""

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    login: str = Field(min_length=1)
    roles: frozenset[str]

    def caller(self) -> McpCaller:
        """Вызывающий приложения для клиента MCP."""
        return McpCaller(login=self.login, roles=self.roles)


@dataclass(frozen=True)
class ServerRecord:
    """Сервер, выданный зрителю: строка mcp_servers и её конфиг для клиента.

    Приложений у серверов из таблицы нет: признак приложений приходит от
    самого сервера при подключении.
    """

    server_id: UUID
    name: str
    origin: McpOrigin
    editable: bool
    enabled: bool
    config: McpServerConfig


class ServerSource(ServerConfigs, ABC):
    """Набор серверов зрителя и их конфиги — единственный вход к серверам чата.

    Им пользуются McpDirectory (список, карточка, правка своих серверов),
    клиент MCP (ServerConfigs) и запуск агента (набор серверов треда).
    Реализация — StoredServers над таблицей mcp_servers; стенды, где серверы
    заданы кодом, держат свою.
    """

    @abstractmethod
    async def visible(self, viewer: McpViewer) -> Sequence[ServerRecord]:
        """Серверы, выданные зрителю лично или через его роли, в порядке имён."""

    @abstractmethod
    async def record(self, viewer: McpViewer, name: str) -> ServerRecord:
        """Запись сервера name из набора зрителя."""

    @abstractmethod
    async def add(
        self, viewer: McpViewer, name: str, server: StoredServer
    ) -> ServerRecord:
        """Создаёт сервер с личной выдачей зрителю."""

    @abstractmethod
    async def replace(
        self, viewer: McpViewer, current: str, name: str, server: StoredServer
    ) -> ServerRecord:
        """Заменяет имя и описание собственного сервера."""

    @abstractmethod
    async def set_enabled(
        self, viewer: McpViewer, name: str, enabled: bool
    ) -> ServerRecord:
        """Меняет доступность собственного сервера."""

    @abstractmethod
    async def remove(self, viewer: McpViewer, name: str) -> None:
        """Удаляет собственный сервер вместе с выдачами."""

    async def enabled(self, viewer: McpViewer) -> Sequence[ServerRecord]:
        """Записи включённых серверов набора зрителя в порядке имён: сервер,
        выключенный владельцем, треду не объявляется.
        """
        records = await self.visible(viewer)

        return list(self._enabled(records))

    async def enabled_names(self, viewer: McpViewer) -> Sequence[str]:
        """Имена включённых серверов набора зрителя в порядке имён."""
        names: list[str] = []
        for record in await self.enabled(viewer):
            names.append(record.name)

        return names

    @staticmethod
    def _enabled(records: Sequence[ServerRecord]) -> Iterator[ServerRecord]:
        for record in records:
            if not record.enabled:
                continue

            yield record


class StoredServers(ServerSource):
    """Серверы зрителя и их конфиги из таблицы mcp_servers.

    Создаётся провайдером процесса. Выдачу и владение проверяет здесь:
    клиент отдаёт конфиг только выданному серверу, а править и удалять
    можно лишь выданный лично.
    """

    def __init__(
        self,
        store: McpServerStore,
        users: UserRows,
        oauth: McpBrowserOAuth | None = None,
    ) -> None:
        self._store = store
        self._users = users
        self._oauth = oauth

    async def visible(self, viewer: McpViewer) -> Sequence[ServerRecord]:
        """Серверы, выданные зрителю лично или через его роли, в порядке имён.

        Ошибки:
        McpAppError — строки mcp_servers не прочитаны.
        """
        try:
            stored = await self._store.visible_to(viewer.user_id, viewer.roles)
        except McpServerStoreError as exc:
            msg = (
                f"the MCP servers app of {viewer.login!r} did not read the servers "
                f"granted to it: {exc}"
            )
            raise McpAppError(msg) from exc

        records: list[ServerRecord] = []
        for server in stored:
            records.append(self._record(server))

        return records

    async def record(self, viewer: McpViewer, name: str) -> ServerRecord:
        """Запись сервера name из набора зрителя.

        Ошибки:
        McpAppError — сервера нет в наборе зрителя либо набор не прочитан.
        """
        records = await self.visible(viewer)
        for record in records:
            if record.name == name:
                return record

        names: list[str] = []
        for record in records:
            names.append(record.name)

        listed = ", ".join(names)
        if not listed:
            listed = "none"

        msg = (
            f"the MCP servers app of {viewer.login!r} did not find the server "
            f"{name!r}: it is not granted to this user, the servers granted are "
            f"{listed}"
        )
        raise McpAppError(msg)

    async def config_of(self, name: str, caller: McpCaller) -> McpServerConfig | None:
        """Конфиг сервера name для клиента; None — сервер не выдан вызывающему.

        Ошибки:
        McpClientError — строка users вызывающего или запись сервера не
            прочитана.
        """
        user_id = await self._user_id(caller)
        try:
            stored = await self._store.granted(name, user_id, caller.roles)
        except McpServerStoreError as exc:
            msg = (
                f"mcp servers of {caller.login!r}: reading the server {name!r} "
                f"failed: {exc}"
            )
            raise McpClientError(msg) from exc

        if stored is None:
            return None

        if not stored.enabled:
            return None

        return self._config(stored.server, stored.id, user_id)

    async def add(
        self, viewer: McpViewer, name: str, server: StoredServer
    ) -> ServerRecord:
        """Новый сервер зрителя, выданный ему лично.

        Ошибки:
        McpAppError — имя занято либо строка не сохранена.
        """
        try:
            server_id = await self._store.add(name, server, viewer.user_id)
        except McpServerStoreError as exc:
            msg = (
                f"the MCP servers app of {viewer.login!r} did not add the server "
                f"{name!r}: {exc}"
            )
            raise McpAppError(msg) from exc

        return ServerRecord(
            server_id=server_id,
            name=name,
            origin=McpOrigin.USER,
            editable=True,
            enabled=True,
            config=self._config(server),
        )

    async def replace(
        self, viewer: McpViewer, current: str, name: str, server: StoredServer
    ) -> ServerRecord:
        """Новое имя и содержимое своего сервера; выдачи и флаг enabled не
        трогает.

        Ошибки:
        McpAppError — сервер не выдан зрителю лично, имя занято либо строка
            не сохранена.
        """
        record = await self._own(viewer, current)
        try:
            if self._oauth is None:
                await self._store.replace(record.server_id, name, server)
            else:
                async with self._oauth.editing(record.server_id):
                    await self._store.replace(record.server_id, name, server)
        except McpServerStoreError as exc:
            msg = (
                f"the MCP servers app of {viewer.login!r} did not replace the "
                f"server {current!r} with {name!r}: {exc}"
            )
            raise McpAppError(msg) from exc

        return replace(record, name=name, config=self._config(server))

    async def set_enabled(
        self, viewer: McpViewer, name: str, enabled: bool
    ) -> ServerRecord:
        """Включение и выключение своего сервера.

        Ошибки:
        McpAppError — сервер не выдан зрителю лично либо строка не сохранена.
        """
        record = await self._own(viewer, name)
        try:
            await self._store.set_enabled(record.server_id, enabled)
        except McpServerStoreError as exc:
            msg = (
                f"the MCP servers app of {viewer.login!r} did not set the server "
                f"{name!r} to enabled={enabled}: {exc}"
            )
            raise McpAppError(msg) from exc

        return replace(record, enabled=enabled)

    async def remove(self, viewer: McpViewer, name: str) -> None:
        """Удаление своего сервера вместе с выдачами.

        Ошибки:
        McpAppError — сервер не выдан зрителю лично либо строка не удалена.
        """
        record = await self._own(viewer, name)
        try:
            await self._store.remove(record.server_id)
        except McpServerStoreError as exc:
            msg = (
                f"the MCP servers app of {viewer.login!r} did not remove the "
                f"server {name!r}: {exc}"
            )
            raise McpAppError(msg) from exc

    async def _own(self, viewer: McpViewer, name: str) -> ServerRecord:
        """Запись сервера, выданного зрителю лично.

        Ошибки:
        McpAppError — сервера нет в наборе зрителя либо он выдан ролью.
        """
        record = await self.record(viewer, name)
        if record.editable:
            return record

        msg = (
            f"the MCP servers app of {viewer.login!r} did not change the server "
            f"{name!r}: it is granted to a role of the user, expected a server "
            "granted to the user personally"
        )
        raise McpAppError(msg)

    async def _user_id(self, caller: McpCaller) -> UUID:
        """Строка users вызывающего по его логину.

        Ошибки:
        McpClientError — строки нет или она не прочитана.
        """
        try:
            stored = await self._users.stored(Login(caller.login))
        except Exception as exc:
            msg = (
                f"mcp servers of {caller.login!r}: reading the users row failed: {exc}"
            )
            raise McpClientError(msg) from exc

        if stored is None:
            msg = (
                f"mcp servers of {caller.login!r}: the user has no users row, so "
                "no server is granted to them"
            )
            raise McpClientError(msg)

        try:
            user = stored.authenticated()
        except AuthenticationError as exc:
            msg = f"mcp servers of {caller.login!r}: the user is not identified: {exc}"
            raise McpClientError(msg) from exc

        return user.id

    def _record(self, stored: StoredMcpServer) -> ServerRecord:
        origin = McpOrigin.ROLE
        if stored.editable:
            origin = McpOrigin.USER

        return ServerRecord(
            server_id=stored.id,
            name=stored.name,
            origin=origin,
            editable=stored.editable,
            enabled=stored.enabled,
            config=self._config(stored.server),
        )

    def _config(
        self,
        server: StoredServer,
        server_id: UUID | None = None,
        user_id: UUID | None = None,
    ) -> McpServerConfig:
        endpoint = server.endpoint
        auth = endpoint.auth
        if isinstance(auth, BrowserOAuthAuth) and server_id and user_id:
            if self._oauth is None:
                raise McpClientError("browser OAuth provider is not configured")
            endpoint = endpoint.model_copy(
                update={"auth": auth.bound(self._oauth, server_id, user_id)}
            )
        return McpServerConfig(endpoint=endpoint, limits=server.limits)
