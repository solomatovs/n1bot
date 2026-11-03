"""Хранилище вложений чата на сервере MCP с расширением файлов: своих
файлов у чата нет.

Файлы пользователя лежат в его workspace на сервере MCP, объявившем
расширение файлов (FilesFeature). Чат пишет и читает их потоком через
клиента MCP от имени владельца файла: владельца называет ключ объекта,
сервер — первый из выданных пользователю в таблице mcp_servers,
объявивший расширение.

Ошибки:
StorageError — владельца или выданные ему серверы не определить, сервер
    недоступен либо отказал.
StorageAbsentError — пользователю не выдан ни один сервер или ни один его
    сервер не объявляет расширения файлов: хранилища у пользователя нет.
StorageNotFoundError — файла нет в workspace.
StorageFullError — в workspace не осталось места.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import UUID

from boba.aid.infra.session import ChainlitSession, OwnerSessions
from boba.aid.mcp_servers import McpAppError
from boba.aid.mcp_servers.sources import McpViewer, ServerSource
from boba.canvas.keys import ObjectKey
from boba.canvas.storage import (
    FileStat,
    OpenedStream,
    StorageAbsentError,
    StorageError,
)
from boba.identity.api import UserRows
from boba.identity.errors import AuthenticationError
from boba.mcp_client.client import FilesHost, McpCaller, McpClientError, McpServers
from boba.runtime.storage import StorageClient
from boba.workspace.launcher import ReadWindow

__all__ = ["FileOwners", "FilesTarget", "RemoteStorageClient"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilesTarget:
    """Где искать файлы и чьим именем: серверы, выданные пользователю, по
    порядку имён и сам пользователь на них; хранилище — первый из серверов,
    объявивший расширение файлов."""

    servers: Sequence[str]
    caller: McpCaller


class FileOwners:
    """Владелец файла и серверы, выданные ему, по ключу объекта.

    Создаётся сборкой чата; им пользуется RemoteStorageClient. Ключ называет
    пользователя (строка users) и тред. Логин и роли берутся из живой сессии
    пользователя — сначала той, что открыла этот тред; без живой сессии
    (ссылка открыта после закрытия вкладки) — из строки users, где их
    оставил последний вход. Серверы — включённые записи таблицы mcp_servers,
    выданные пользователю, в порядке имён.
    """

    def __init__(
        self,
        sessions: Callable[[], OwnerSessions],
        users: Callable[[], UserRows],
        source: Callable[[], ServerSource],
    ) -> None:
        self._sessions = sessions
        self._users = users
        self._source = source

    async def of_thread(self, user_id: str, thread_id: str) -> FilesTarget:
        session = self._session_of(user_id, thread_id)
        if session is not None:
            viewer = McpViewer(
                user_id=UUID(user_id),
                login=session.identifier,
                roles=session.roles,
            )
            servers = await self._granted(viewer, thread_id)

            return FilesTarget(servers, viewer.caller())

        try:
            stored = await self._users().stored_by_id(UUID(user_id))
        except ValueError as exc:
            msg = (
                f"files of thread {thread_id}: the owner {user_id!r} is not a "
                f"users id: {exc}"
            )
            raise StorageError(msg) from exc

        if stored is None:
            msg = f"files of thread {thread_id}: no users row with id {user_id!r}"
            raise StorageError(msg)

        try:
            user = stored.authenticated()
        except AuthenticationError as exc:
            msg = f"files of thread {thread_id}: the owner is not identified: {exc}"
            raise StorageError(msg) from exc

        viewer = McpViewer(
            user_id=user.id,
            login=user.identifier,
            roles=user.sign_in.roles,
        )
        servers = await self._granted(viewer, thread_id)

        return FilesTarget(servers, viewer.caller())

    def _session_of(self, user_id: str, thread_id: str) -> ChainlitSession | None:
        """Живая сессия владельца: открывшая этот тред, иначе любая его."""
        try:
            owner = UUID(user_id)
        except ValueError:
            return None

        found = self._sessions().of_user(owner)
        for session in found:
            if session.thread_id == thread_id:
                return session

        if found:
            return found[0]

        return None

    async def _granted(self, viewer: McpViewer, thread_id: str) -> Sequence[str]:
        """Имена включённых серверов, выданных зрителю.

        Ошибки:
        StorageError — строки mcp_servers не прочитаны.
        StorageAbsentError — зрителю не выдан ни один сервер: хранилища
            файлов у него нет.
        """
        try:
            names = await self._source().enabled_names(viewer)
        except McpAppError as exc:
            msg = (
                f"files of thread {thread_id} of {viewer.login!r}: reading the "
                f"mcp servers granted to the user failed: {exc}"
            )
            raise StorageError(msg) from exc

        if not names:
            msg = (
                f"files of thread {thread_id}: no mcp server is granted to the "
                f"user {viewer.login!r}, so the thread has no file storage"
            )
            raise StorageAbsentError(msg)

        return names


class RemoteStorageClient(StorageClient):
    """Реализация StorageClient поверх файлов workspace сервера MCP,
    объявившего расширение файлов.

    Создаётся сборкой чата из источника владельцев (FileOwners) и клиента
    MCP-серверов. Каждая операция идёт от имени владельца ключа на первый
    сервер его профиля с расширением файлов; тело файла течёт потоком в обе
    стороны и в чате не оседает.
    """

    CHUNK_BYTES: ClassVar[int] = 1024 * 1024

    def __init__(
        self,
        public_prefix: str,
        owners: FileOwners,
        servers: Callable[[], McpServers],
    ) -> None:
        super().__init__(public_prefix, self.CHUNK_BYTES)
        self._owners = owners
        self._servers = servers

    async def workspace(self, user_id: str, thread_id: str) -> str:
        """Каталог workspace, который объявил сервер файлов треда.

        Ошибки:
        StorageError — сервер профиля недоступен.
        StorageAbsentError — хранилища файлов у пользователя нет.
        """
        host = await self._host(user_id, thread_id)

        return host.workspace

    async def _upload_stream(
        self,
        object_key: str,
        source: AsyncIterator[bytes],
        mime: str,
    ) -> dict[str, Any]:
        key = ObjectKey.parse(object_key)
        host = await self._host(key.user_id, key.scope_id)
        await host.files.upload(key, source)

        return self._uploaded(object_key)

    async def _stat(self, object_key: str) -> FileStat:
        key = ObjectKey.parse(object_key)
        host = await self._host(key.user_id, key.scope_id)

        return await host.files.stat(key)

    async def _open_stream(self, object_key: str, window: ReadWindow) -> OpenedStream:
        key = ObjectKey.parse(object_key)
        host = await self._host(key.user_id, key.scope_id)

        return await host.files.open(key, window)

    async def _delete_file(self, object_key: str) -> bool:
        key = ObjectKey.parse(object_key)
        host = await self._host(key.user_id, key.scope_id)

        return await host.files.delete(key)

    async def _list_dir(self, prefix: str) -> Sequence[str]:
        msg = (
            f"listing {prefix!r}: the file storage of the mcp server has no "
            "directory listing"
        )
        raise StorageError(msg)

    async def _delete_scope(self, user_id: str, scope_id: str) -> None:
        """Область треда одной операцией сервиса; у пользователя без
        хранилища файлов нет — удалять нечего."""
        try:
            host = await self._host(user_id, scope_id)
        except StorageAbsentError as exc:
            logger.info(
                "scope %s of user %s: no file storage, nothing to delete: %s",
                scope_id,
                user_id,
                exc,
            )
            return

        await host.files.delete_scope(scope_id)

    async def _host(self, user_id: str, thread_id: str) -> FilesHost:
        """Хранилище файлов треда: первый сервер профиля владельца, объявивший
        расширение файлов."""
        target = await self._owners.of_thread(user_id, thread_id)
        try:
            host = await self._servers().files_host(target.servers, target.caller)
        except McpClientError as exc:
            msg = (
                f"files of thread {thread_id} of user {user_id}: an mcp server of "
                f"{list(target.servers)} is unavailable for "
                f"{target.caller.login!r}: {exc}"
            )
            raise StorageError(msg) from exc

        if host is None:
            msg = (
                f"files of thread {thread_id} of user {user_id}: none of the mcp "
                f"servers {list(target.servers)} declares the files extension, so "
                "the user has no file storage"
            )
            raise StorageAbsentError(msg)

        return host
