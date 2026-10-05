"""Хранилище вложений чата на сервере boba-mcp: своих файлов у чата нет.

Файлы пользователя лежат в его workspace на сервере инструментов. Чат
пишет и читает их потоком через клиента MCP от имени владельца файла:
владельца называет ключ объекта, сервер — первый MCP-сервер профиля, в
котором пользователь работает.

Ошибки:
StorageError — владельца или сервер файлов не определить, сервер недоступен
    либо отказал.
StorageNotFoundError — файла нет в workspace.
StorageFullError — в workspace не осталось места.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import UUID

from boba.canvas.keys import ObjectKey
from boba.canvas.storage import FileStat, OpenedStream, StorageError
from boba.chainlit.infra.session import ChainlitSession, OwnerSessions
from boba.chat.profiles import ChatProfiles
from boba.identity.api import UserRows
from boba.identity.signin import SignInMetadata
from boba.mcp_client.client import McpCaller, McpClientError, McpFiles, McpServers
from boba.runtime.storage import StorageClient
from boba.workspace.launcher import ReadWindow

__all__ = ["FileOwners", "FilesTarget", "RemoteStorageClient"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilesTarget:
    """Где и чьим именем лежат файлы: сервер MCP и пользователь на нём."""

    server: str
    caller: McpCaller


class FileOwners:
    """Владелец файла и его сервер файлов по ключу объекта.

    Создаётся сборкой чата; им пользуется RemoteStorageClient. Ключ называет
    пользователя (строка users) и тред. Логин, роли и профиль берутся из
    живой сессии пользователя — сначала той, что открыла этот тред; без
    живой сессии (ссылка открыта после закрытия вкладки) — из строки users,
    где их оставил последний вход. Сервер файлов — первый MCP-сервер профиля.
    """

    def __init__(
        self,
        sessions: Callable[[], OwnerSessions],
        users: Callable[[], UserRows],
        profiles: ChatProfiles,
    ) -> None:
        self._sessions = sessions
        self._users = users
        self._profiles = profiles

    async def of_key(self, key: ObjectKey) -> FilesTarget:
        session = self._session_of(key)
        if session is not None:
            caller = McpCaller(login=session.identifier, roles=session.roles)

            return FilesTarget(self._server_of(session.chat_profile, key), caller)

        try:
            stored = await self._users().stored_by_id(UUID(key.user_id))
        except ValueError as exc:
            msg = (
                f"files of {key.render()}: the key names user {key.user_id!r}, "
                f"expected a users id: {exc}"
            )
            raise StorageError(msg) from exc

        if stored is None:
            msg = f"files of {key.render()}: no users row with id {key.user_id!r}"
            raise StorageError(msg)

        sign_in = SignInMetadata.parse(stored.meta)
        caller = McpCaller(login=stored.identifier, roles=sign_in.roles)

        return FilesTarget(self._granted_server(sign_in, key), caller)

    def _session_of(self, key: ObjectKey) -> ChainlitSession | None:
        """Живая сессия владельца: открывшая тред ключа, иначе любая его."""
        try:
            user_id = UUID(key.user_id)
        except ValueError:
            return None

        found = self._sessions().of_user(user_id)
        for session in found:
            if session.thread_id == key.thread_id:
                return session

        if found:
            return found[0]

        return None

    def _server_of(self, profile: str | None, key: ObjectKey) -> str:
        declared = self._profiles.all
        if not profile or profile not in declared:
            msg = (
                f"files of {key.render()}: the session works in profile "
                f"{profile!r}, expected one of {sorted(declared)}"
            )
            raise StorageError(msg)

        servers = declared[profile].mcp
        if not servers:
            msg = (
                f"files of {key.render()}: profile {profile!r} names no mcp "
                "server, so it has no file storage"
            )
            raise StorageError(msg)

        return servers[0]

    def _granted_server(self, sign_in: SignInMetadata, key: ObjectKey) -> str:
        """Сервер файлов пользователя без живой сессии: первый сервер первого
        профиля, выданного его последнему входу."""
        for config in self._profiles.visible_for(sign_in.profiles).values():
            if config.mcp:
                return config.mcp[0]

        msg = (
            f"files of {key.render()}: none of the profiles "
            f"{sorted(sign_in.profiles)} granted to the user names an mcp server"
        )
        raise StorageError(msg)


class RemoteStorageClient(StorageClient):
    """Реализация StorageClient поверх файлов workspace сервера boba-mcp.

    Создаётся сборкой чата из источника владельцев (FileOwners) и клиента
    MCP-серверов. Каждая операция идёт от имени владельца ключа на его
    сервер файлов; тело файла течёт потоком в обе стороны и в чате не
    оседает.
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

    async def _upload_stream(
        self,
        object_key: str,
        source: AsyncIterator[bytes],
        mime: str,
    ) -> dict[str, Any]:
        key = ObjectKey.parse(object_key)
        files = await self._files(key)
        await files.upload(key, source)

        return self._uploaded(object_key)

    async def _stat(self, object_key: str) -> FileStat:
        key = ObjectKey.parse(object_key)
        files = await self._files(key)

        return await files.stat(key)

    async def _open_stream(self, object_key: str, window: ReadWindow) -> OpenedStream:
        key = ObjectKey.parse(object_key)
        files = await self._files(key)

        return await files.open(key, window)

    async def _delete_file(self, object_key: str) -> bool:
        key = ObjectKey.parse(object_key)
        files = await self._files(key)

        return await files.delete(key)

    async def _list_dir(self, prefix: str) -> Sequence[str]:
        msg = (
            f"listing {prefix!r}: the file storage of the mcp server has no "
            "directory listing"
        )
        raise StorageError(msg)

    async def _files(self, key: ObjectKey) -> McpFiles:
        target = await self._owners.of_key(key)
        try:
            files = await self._servers().files(target.server, target.caller)
        except McpClientError as exc:
            msg = (
                f"files of {key.render()}: mcp server {target.server!r} is "
                f"unavailable for {target.caller.login!r}: {exc}"
            )
            raise StorageError(msg) from exc

        if files is None:
            msg = (
                f"files of {key.render()}: mcp server {target.server!r} is "
                "unavailable or declares no file storage"
            )
            raise StorageError(msg)

        return files
