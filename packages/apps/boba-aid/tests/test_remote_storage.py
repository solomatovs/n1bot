"""Хранилище чата поверх файлов workspace сервера boba-mcp: настоящий сервис
отдельным процессом, чат ходит к нему клиентом MCP от имени владельца файла;
удаление треда удаляет у сервиса область целиком — файлы и журналы вызовов."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import mcp_types as mt
import pytest
from aid_stand import FixedServers, NoTabs, OwnerRows, ServiceProcess

from boba.aid.data.remote_storage import FileOwners, RemoteStorageClient
from boba.aid.mcp_servers.sources import ServerSource
from boba.canvas.keys import ObjectKey, ScopeDir
from boba.canvas.storage import StorageError, StorageNotFoundError
from boba.identity.api import StoredUser
from boba.identity.session import Login, UserMetadataField
from boba.mcp_client.client import McpCaller, McpServers, McpToolServer
from boba.toolrun.call_id import CallFields
from boba.workspace.launcher import ReadWindow

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

REPO = Path(__file__).resolve().parents[4]
USER = UUID("11111111-2222-4333-8444-555555555555")
THREAD = "7a1c0d5e-2b3f-4a6b-9c8d-0e1f2a3b4c5d"
OTHER = "0b2d1e6f-3c4a-4b7c-8d9e-1f2a3b4c5d6e"


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ServiceProcess]:
    process = ServiceProcess(tmp_path_factory.mktemp("boba-mcp"))
    try:
        process.await_listening()
        yield process
    finally:
        process.stop()


@pytest.fixture
async def servers(service: ServiceProcess) -> AsyncIterator[McpServers]:
    opened = McpServers(service.servers())
    try:
        yield opened
    finally:
        await opened.stop()


@pytest.fixture
def granted(service: ServiceProcess) -> FixedServers:
    """Сервер сервиса стенда, выданный владельцу файлов."""
    return FixedServers(service.servers().servers)


@pytest.fixture
def nothing_granted() -> FixedServers:
    """Источник без серверов: владельцу файлов не выдан ни один."""
    return FixedServers({})


def _storage(
    servers: McpServers, source: ServerSource, meta: Mapping[str, Any]
) -> RemoteStorageClient:
    sessions = NoTabs()
    users = OwnerRows(
        StoredUser(
            id=USER,
            identifier=Login("alice"),
            created_at=datetime.now(UTC),
            meta=meta,
        )
    )
    owners = FileOwners(lambda: sessions, lambda: users, lambda: source)

    return RemoteStorageClient("/workspace", owners, lambda: servers)


def _signed_in() -> dict[str, Any]:
    return {UserMetadataField.ROLES: ["dev"]}


async def _source(payload: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(payload), 40000):
        yield payload[start : start + 40000]


class TestRemoteStorage:
    async def test_file_goes_to_the_server_and_back_as_a_stream(
        self, servers: McpServers, granted: FixedServers
    ) -> None:
        storage = _storage(servers, granted, _signed_in())
        payload = bytes(range(256)) * 1000
        key = ObjectKey(user_id=str(USER), scope_id=THREAD, name="data.bin")

        await storage.upload_stream(key.render(), _source(payload))
        stat = await storage.stat(key.render())
        async with await storage.open_stream(
            key.render(), ReadWindow(offset=10, length=20)
        ) as part:
            window = b"".join([chunk async for chunk in part.chunks])

        if stat.size != len(payload):
            raise AssertionError(f"the size is the size on the server: {stat}")
        if window != payload[10:30]:
            raise AssertionError("the window is read from the server")
        if not await storage.delete_file(key.render()):
            raise AssertionError("the file is deleted on the server")

    async def test_missing_file_is_not_found(
        self, servers: McpServers, granted: FixedServers
    ) -> None:
        storage = _storage(servers, granted, _signed_in())
        key = ObjectKey(
            user_id=str(USER), scope_id=THREAD, name="ghost.mmd", dir=ScopeDir.MERMAID
        )

        with pytest.raises(StorageNotFoundError):
            await storage.stat(key.render())

    async def test_user_without_a_files_server_has_no_storage(
        self, servers: McpServers, nothing_granted: FixedServers
    ) -> None:
        """Ни один сервер не выдан пользователю строкой таблицы: хранилища
        файлов у него нет."""
        storage = _storage(servers, nothing_granted, _signed_in())
        key = ObjectKey(user_id=str(USER), scope_id=THREAD, name="data.bin")

        with pytest.raises(StorageError, match="no mcp server is granted"):
            await storage.stat(key.render())

    async def test_unknown_owner_is_refused(
        self, servers: McpServers, granted: FixedServers
    ) -> None:
        storage = _storage(servers, granted, _signed_in())
        stranger = "99999999-2222-4333-8444-555555555555"
        key = ObjectKey(user_id=stranger, scope_id=THREAD, name="data.bin")

        with pytest.raises(StorageError, match="no users row"):
            await storage.stat(key.render())

    async def test_deleting_the_thread_removes_its_files_and_call_journals(
        self, servers: McpServers, service: ServiceProcess, granted: FixedServers
    ) -> None:
        """Тред удаляется одной операцией сервиса: файлы его каталогов и
        журналы вызовов, которые шли в нём; журналы другого треда целы."""
        storage = _storage(servers, granted, _signed_in())
        key = ObjectKey(user_id=str(USER), scope_id=THREAD, name="gone.bin")
        await storage.upload_stream(key.render(), _source(b"gone"))
        caller = McpCaller(login="alice", roles=frozenset({"dev"}))
        port = await servers.port("boba", caller)
        journals = service.journal_dir / ServiceProcess.OWNER
        own = await self._journaled(port, THREAD, journals)
        other = await self._journaled(port, OTHER, journals)

        await storage.delete_scope(str(USER), THREAD)

        with pytest.raises(StorageNotFoundError):
            await storage.stat(key.render())
        if own.exists():
            raise AssertionError(f"the call journal {own} of the thread is removed")
        if not other.exists():
            raise AssertionError(f"the call journal {other} of another thread is kept")

    @staticmethod
    async def _journaled(port: McpToolServer, thread_id: str, journals: Path) -> Path:
        """Вызов инструмента сервиса в треде thread_id; итог — каталог
        журнала его запуска у сервиса."""
        arguments = {"text": "hi", "repeat": 1, CallFields.SCOPE: thread_id}
        params = mt.CallToolRequestParams(name="fake_echo", arguments=arguments)
        reply = await port.call(McpServers.OWN, params, None, None, None, None)
        if not isinstance(reply, mt.CallToolResult) or reply.is_error:
            raise AssertionError(f"fake_echo answers in thread {thread_id}: {reply}")

        runs: list[Path] = []
        for block in reply.content:
            if not isinstance(block, mt.ResourceLink):
                continue

            run = urlsplit(str(block.uri)).netloc
            runs.append(journals / run)

        if not runs:
            raise AssertionError(f"the call links its journal: {reply.content}")

        if not runs[0].is_dir():
            raise AssertionError(f"the journal of the call lies in {runs[0]}")

        return runs[0]
