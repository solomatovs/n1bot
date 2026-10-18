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
from aid_stand import NoTabs, OwnerRows, ServiceProcess

from boba.aid.data.remote_storage import FileOwners, RemoteStorageClient
from boba.aid.rendering.acp_view import ServerCards, ToolMedia
from boba.canvas.keys import ObjectKey, ThreadDir
from boba.canvas.storage import StorageError, StorageNotFoundError
from boba.chat.profiles import ChatProfileConfig, ChatProfiles
from boba.identity.api import StoredUser
from boba.identity.session import Login, UserMetadataField
from boba.mcp_client.client import McpCaller, McpServers, McpToolServer
from boba.toolkit.mcp import McpToolNames
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


def _storage(servers: McpServers, meta: Mapping[str, Any]) -> RemoteStorageClient:
    profiles = ChatProfiles(
        {
            "files": ChatProfileConfig.model_construct(
                mcp=["boba"], roles=["*"], default=True
            ),
            "bare": ChatProfileConfig.model_construct(
                mcp=[], roles=["*"], default=False
            ),
        }
    )
    sessions = NoTabs()
    users = OwnerRows(
        StoredUser(
            id=USER,
            identifier=Login("alice"),
            created_at=datetime.now(UTC),
            meta=meta,
        )
    )
    owners = FileOwners(lambda: sessions, lambda: users, profiles)

    return RemoteStorageClient("/workspace", owners, lambda: servers)


def _signed_in(*profiles: str) -> dict[str, Any]:
    return {
        UserMetadataField.ROLES: ["dev"],
        UserMetadataField.PROFILES: list(profiles),
    }


async def _source(payload: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(payload), 40000):
        yield payload[start : start + 40000]


class TestRemoteStorage:
    async def test_file_goes_to_the_server_and_back_as_a_stream(
        self, servers: McpServers
    ) -> None:
        storage = _storage(servers, _signed_in("files"))
        payload = bytes(range(256)) * 1000
        key = ObjectKey(user_id=str(USER), thread_id=THREAD, name="data.bin")

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

    async def test_missing_file_is_not_found(self, servers: McpServers) -> None:
        storage = _storage(servers, _signed_in("files"))
        key = ObjectKey(
            user_id=str(USER), thread_id=THREAD, name="ghost.mmd", dir=ThreadDir.MERMAID
        )

        with pytest.raises(StorageNotFoundError):
            await storage.stat(key.render())

    async def test_user_without_a_files_server_has_no_storage(
        self, servers: McpServers
    ) -> None:
        storage = _storage(servers, _signed_in("bare"))
        key = ObjectKey(user_id=str(USER), thread_id=THREAD, name="data.bin")

        with pytest.raises(StorageError, match="names an mcp server"):
            await storage.stat(key.render())

    async def test_unknown_owner_is_refused(self, servers: McpServers) -> None:
        storage = _storage(servers, _signed_in("files"))
        stranger = "99999999-2222-4333-8444-555555555555"
        key = ObjectKey(user_id=stranger, thread_id=THREAD, name="data.bin")

        with pytest.raises(StorageError, match="no users row"):
            await storage.stat(key.render())

    async def test_deleting_the_thread_removes_its_files_and_call_journals(
        self, servers: McpServers, service: ServiceProcess
    ) -> None:
        """Тред удаляется одной операцией сервиса: файлы его каталогов и
        журналы вызовов, которые шли в нём; журналы другого треда целы."""
        storage = _storage(servers, _signed_in("files"))
        key = ObjectKey(user_id=str(USER), thread_id=THREAD, name="gone.bin")
        await storage.upload_stream(key.render(), _source(b"gone"))
        caller = McpCaller(login="alice", roles=frozenset({"dev"}))
        port = await servers.port("boba", caller)
        journals = service.journal_dir / ServiceProcess.OWNER
        own = await self._journaled(port, THREAD, journals)
        other = await self._journaled(port, OTHER, journals)

        await storage.delete_thread(str(USER), THREAD)

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
        arguments = {"text": "hi", "repeat": 1, CallFields.THREAD: thread_id}
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


class TestThreadCards:
    """Карточки инструментов ленты треда — у порта владельца треда через
    кэш ответов: список с cacheScope private одного пользователя не рисует
    вызовы другого."""

    async def test_cards_of_one_user_are_not_drawn_for_another(
        self, servers: McpServers
    ) -> None:
        wide = ToolMedia(
            ServerCards(servers, McpCaller(login="ivanov", roles=frozenset({"dev"})))
        )
        weak = ToolMedia(
            ServerCards(servers, McpCaller(login="petrov", roles=frozenset({"weak"})))
        )
        names = McpToolNames()
        progress = names.full("boba", "fake_progress")
        echo = names.full("boba", "fake_echo")

        wide_card = await wide.card(progress)
        weak_card = await weak.card(progress)
        weak_echo = await weak.card(echo)
        links = await weak.links("boba")

        assert wide_card is not None
        assert weak_card is None
        assert weak_echo is not None
        assert links is not None
