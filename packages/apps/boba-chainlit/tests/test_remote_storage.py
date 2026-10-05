"""Хранилище чата поверх файлов workspace сервера boba-mcp: настоящий сервис
отдельным процессом, чат ходит к нему клиентом MCP от имени владельца файла."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID

import pytest

from boba.canvas.keys import ObjectKey, ThreadDir
from boba.canvas.storage import StorageError, StorageNotFoundError
from boba.chainlit.data.remote_storage import FileOwners, RemoteStorageClient
from boba.chainlit.infra.session import ChainlitSession, OwnerSessions
from boba.chat.profiles import ChatProfileConfig, ChatProfiles
from boba.identity.api import StoredUser, UserRows
from boba.identity.context import CallContexts
from boba.identity.session import Login, UserMetadataField
from boba.mcp_client.client import (
    DroppedSignals,
    McpServers,
    McpServersConfig,
    NamedBlocks,
)
from boba.workspace.launcher import ReadWindow

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

REPO = Path(__file__).resolve().parents[4]
USER = UUID("11111111-2222-4333-8444-555555555555")
THREAD = "7a1c0d5e-2b3f-4a6b-9c8d-0e1f2a3b4c5d"


class ServiceProcess:
    """Стенд сервиса boba-mcp отдельным процессом; вход — proxy."""

    STAND: ClassVar[Path] = REPO / "packages/apps/boba-mcp/tests/service_stand.py"
    SECRET: ClassVar[str] = "stand-proxy-secret"

    def __init__(self, workdir: Path) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = int(probe.getsockname()[1])

        self._process = subprocess.Popen(
            [sys.executable, str(self.STAND), str(self.port), str(workdir)]
        )

    def await_listening(self) -> None:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1.0):
                    return
            except OSError:
                time.sleep(0.2)

        raise AssertionError(f"the service did not listen on port {self.port}")

    def servers(self) -> McpServersConfig:
        location = {"scheme": "http", "host": "127.0.0.1", "port": self.port}
        endpoint = {
            "transport": "streamable-http",
            **location,
            "path": "/mcp/service",
            "auth": {
                "auth": "proxy",
                "secret": self.SECRET,
                "sign_in": {**location, "path": "/auth/proxy"},
                "headers": {
                    "user": "X-Remote-User",
                    "timestamp": "X-Boba-Timestamp",
                    "signature": "X-Boba-Signature",
                    "roles": "X-Remote-Roles",
                },
            },
        }
        server = {
            "endpoint": endpoint,
            "connect_timeout_sec": 30.0,
            "call_timeout_sec": 60.0,
        }

        return McpServersConfig.model_validate({"servers": {"boba": server}})

    def stop(self) -> None:
        self._process.terminate()
        self._process.wait(timeout=10)


class NoSessions(OwnerSessions):
    """Источник сессий без живых вкладок: владелец ищется по строке users."""

    def of_user(self, user_id: UUID) -> Sequence[ChainlitSession]:
        return ()


class KnownUsers(UserRows):
    """Строки users стенда: один пользователь с ролями и профилями входа."""

    def __init__(self, meta: Mapping[str, Any]) -> None:
        self._meta = dict(meta)

    async def stored(self, identifier: Login) -> StoredUser | None:
        return None

    async def stored_by_id(self, user_id: UUID) -> StoredUser | None:
        if user_id != USER:
            return None

        return StoredUser(
            id=USER,
            identifier=Login("alice"),
            created_at=datetime.now(UTC),
            meta=self._meta,
        )

    async def upsert(self, identifier: Login, meta: Mapping[str, Any]) -> StoredUser:
        raise NotImplementedError

    async def set_llm_settings(
        self, user_id: UUID, profile: str, values: Mapping[str, Any]
    ) -> None:
        raise NotImplementedError


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
    opened = McpServers(
        service.servers(), NamedBlocks(), DroppedSignals(), CallContexts()
    )
    await opened.start()
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
    sessions = NoSessions()
    users = KnownUsers(meta)
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
