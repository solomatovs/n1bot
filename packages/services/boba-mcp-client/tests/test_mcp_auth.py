"""Способы входа клиента MCP на настоящем сервере авторизации сервиса boba:
local — учёткой с паролем через страницу входа без человека, proxy —
утверждением о логине и ролях из самой записи; обновление токена по
истечении срока в обоих."""

from __future__ import annotations

import asyncio
import logging
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from pydantic import SecretStr, TypeAdapter

from boba.identity.context import CallContexts
from boba.mcp_client.client import (
    BearerAuth,
    DroppedSignals,
    HttpEndpoint,
    HttpLocation,
    LocalAuth,
    McpAuth,
    McpAuthEntry,
    McpCaller,
    McpClientError,
    McpServerConfig,
    McpToolServer,
    NamedBlocks,
    NoAuth,
    ProxyAuth,
)
from boba.toolkit.dag import DagNode
from boba.toolrun.hosted import DirectCalls

pytestmark = pytest.mark.anyio


class AuthStand:
    """Сервис boba-mcp стенда отдельным процессом с названным сроком токена
    доступа: клиент видит его только по сети."""

    REPO: ClassVar[Path] = Path(__file__).resolve().parents[4]
    STAND: ClassVar[Path] = (
        REPO / "packages" / "apps" / "boba-mcp" / "tests" / "service_stand.py"
    )
    HOST: ClassVar[str] = "127.0.0.1"
    ENDPOINT: ClassVar[str] = "/mcp/service"
    METADATA: ClassVar[str] = "/.well-known/oauth-authorization-server"
    WAIT_SEC: ClassVar[float] = 60.0

    LOGIN: ClassVar[str] = "carol"
    PASSWORD: ClassVar[str] = "carol-pw"
    ROLELESS: ClassVar[str] = "ghost"
    ROLELESS_PASSWORD: ClassVar[str] = "ghost-pw"
    CLIENT_ID: ClassVar[str] = "stand-chat"
    CLIENT_SECRET: ClassVar[str] = "stand-client-secret"
    PROXY_SECRET: ClassVar[str] = "stand-proxy-secret"

    def __init__(self, workdir: Path, token_ttl_sec: int) -> None:
        with socket.socket() as probe:
            probe.bind((self.HOST, 0))
            self.port: int = probe.getsockname()[1]

        self.token_ttl_sec = token_ttl_sec
        self._process = subprocess.Popen(
            [
                sys.executable,
                str(self.STAND),
                str(self.port),
                str(workdir),
                str(token_ttl_sec),
            ]
        )

    def wait(self) -> None:
        """Ждёт, пока сервер авторизации стенда ответит метаданными."""
        url = f"http://{self.HOST}:{self.port}{self.METADATA}"
        deadline = time.monotonic() + self.WAIT_SEC
        while time.monotonic() < deadline:
            try:
                if httpx.get(url).status_code == httpx.codes.OK:
                    return
            except httpx.TransportError:
                pass

            time.sleep(0.2)

        raise AssertionError(f"the stand service did not answer {url}")

    def stop(self) -> None:
        self._process.terminate()
        self._process.wait(timeout=10)

    def local(self, login: str, password: str) -> LocalAuth:
        return LocalAuth(login=login, password=SecretStr(password))

    def proxy(self, login: str, roles: Sequence[str]) -> ProxyAuth:
        return ProxyAuth(
            issuer=HttpLocation(
                scheme="http", host=self.HOST, port=self.port, path="/"
            ),
            client_id=self.CLIENT_ID,
            client_secret=SecretStr(self.CLIENT_SECRET),
            secret=SecretStr(self.PROXY_SECRET),
            login=login,
            roles=tuple(roles),
        )

    def server(self, auth: McpAuthEntry) -> McpToolServer:
        endpoint = HttpEndpoint.model_validate(
            {
                "scheme": "http",
                "host": self.HOST,
                "port": self.port,
                "path": self.ENDPOINT,
                "auth": auth,
            }
        )
        config = McpServerConfig(
            endpoint=endpoint, connect_timeout_sec=20.0, call_timeout_sec=60.0
        )

        return McpToolServer(
            "boba",
            config,
            NamedBlocks(),
            DroppedSignals(),
            CallContexts(),
            DirectCalls(),
        )


class RequestLog(logging.Handler):
    """Запросы HTTP клиента по журналу httpx2: по ним тест видит, каким путём
    получен токен — страницей входа, обновлением или обменом утверждения."""

    LOGGER: ClassVar[str] = "httpx2"

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []
        self._logger = logging.getLogger(self.LOGGER)
        self._level = self._logger.level

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def start(self) -> None:
        self._logger.setLevel(logging.INFO)
        self._logger.addHandler(self)

    def finish(self) -> None:
        self._logger.removeHandler(self)
        self._logger.setLevel(self._level)

    def count(self, method: str, path: str) -> int:
        found = 0
        for line in self.lines:
            if f"{method} " not in line:
                continue

            if f"{path} " not in line and f"{path}?" not in line:
                continue

            found += 1

        return found


@pytest.fixture
def requests() -> Iterator[RequestLog]:
    log = RequestLog()
    log.start()
    try:
        yield log
    finally:
        log.finish()


@pytest.fixture
def stand(tmp_path: Path) -> Iterator[AuthStand]:
    started = AuthStand(tmp_path, 300)
    try:
        started.wait()
        yield started
    finally:
        started.stop()


@pytest.fixture
def brief(tmp_path: Path) -> Iterator[AuthStand]:
    """Стенд с токеном доступа на две секунды."""
    started = AuthStand(tmp_path, 2)
    try:
        started.wait()
        yield started
    finally:
        started.stop()


def _whoami() -> DagNode:
    return DagNode.model_validate(
        {"key": "call-whoami", "tool": "mcp__boba__fake_whoami", "args": {}}
    )


@pytest.fixture
async def opened() -> AsyncIterator[list[McpToolServer]]:
    """Порты, открытые тестом: закрываются после него."""
    servers: list[McpToolServer] = []
    try:
        yield servers
    finally:
        for server in servers:
            await server.close()


class TestLocalAuth:
    async def test_account_of_the_entry_signs_in_without_a_person(
        self, stand: AuthStand, opened: list[McpToolServer], requests: RequestLog
    ) -> None:
        server = stand.server(stand.local(AuthStand.LOGIN, AuthStand.PASSWORD))
        opened.append(server)

        await server.open()
        message = await server.call(_whoami())

        assert str(message.content).startswith("carol|dev|"), message
        assert requests.count("GET", "/authorize") == 1, requests.lines
        assert requests.count("POST", "/login") == 1, requests.lines
        assert requests.count("POST", "/register") == 1, requests.lines

    async def test_wrong_password_is_a_client_error_with_the_server_reply(
        self, stand: AuthStand, opened: list[McpToolServer]
    ) -> None:
        server = stand.server(stand.local(AuthStand.LOGIN, "not-the-password"))
        opened.append(server)

        with pytest.raises(McpClientError) as caught:
            await server.open()

        text = str(caught.value)
        assert "mcp auth local" in text, text
        assert f"http://127.0.0.1:{stand.port}/login" in text, text
        assert "'carol'" in text, text
        assert "HTTP 401" in text, text
        assert "not-the-password" not in text, text

    async def test_account_without_roles_is_refused_with_403(
        self, stand: AuthStand, opened: list[McpToolServer]
    ) -> None:
        server = stand.server(
            stand.local(AuthStand.ROLELESS, AuthStand.ROLELESS_PASSWORD)
        )
        opened.append(server)

        with pytest.raises(McpClientError) as caught:
            await server.open()

        text = str(caught.value)
        assert "HTTP 403" in text, text
        assert "'ghost'" in text, text

    async def test_expired_token_is_renewed_by_the_refresh_token(
        self, brief: AuthStand, opened: list[McpToolServer], requests: RequestLog
    ) -> None:
        server = brief.server(brief.local(AuthStand.LOGIN, AuthStand.PASSWORD))
        opened.append(server)
        await server.open()
        first = await server.call(_whoami())
        issued = requests.count("POST", "/token")

        await asyncio.sleep(brief.token_ttl_sec + 1.5)
        second = await server.call(_whoami())

        assert str(first.content).startswith("carol|dev|"), first
        assert str(second.content).startswith("carol|dev|"), second
        assert requests.count("POST", "/token") > issued, requests.lines
        assert requests.count("POST", "/login") == 1, requests.lines


class TestProxyAuth:
    async def test_login_and_roles_of_the_entry_reach_the_server(
        self, stand: AuthStand, opened: list[McpToolServer]
    ) -> None:
        server = stand.server(stand.proxy("ivanov", ["dev"]))
        opened.append(server)

        await server.open()
        message = await server.call(_whoami())

        assert str(message.content).startswith("ivanov|dev|"), message

    async def test_entry_takes_the_user_of_the_host(
        self, stand: AuthStand, opened: list[McpToolServer]
    ) -> None:
        declared = stand.proxy("", [])
        caller = McpCaller(login="petrov", roles=frozenset({"dev", "ADM"}))
        server = stand.server(declared.on_behalf_of(caller))
        opened.append(server)

        await server.open()
        message = await server.call(_whoami())

        assert str(message.content).startswith("petrov|ADM,dev|"), message

    async def test_entry_without_a_login_is_a_client_error(
        self, stand: AuthStand, opened: list[McpToolServer]
    ) -> None:
        server = stand.server(stand.proxy("", []))
        opened.append(server)

        with pytest.raises(McpClientError) as caught:
            await server.open()

        assert "names no login" in str(caught.value), caught.value

    async def test_expired_token_is_replaced_by_a_new_exchange(
        self, brief: AuthStand, opened: list[McpToolServer], requests: RequestLog
    ) -> None:
        server = brief.server(brief.proxy("ivanov", ["dev"]))
        opened.append(server)
        await server.open()
        first = await server.call(_whoami())
        issued = requests.count("POST", "/token")

        await asyncio.sleep(brief.token_ttl_sec + 1.5)
        second = await server.call(_whoami())

        assert str(first.content).startswith("ivanov|dev|"), first
        assert str(second.content).startswith("ivanov|dev|"), second
        assert requests.count("POST", "/token") > issued, requests.lines
        assert requests.count("POST", "/login") == 0, requests.lines


class TestAuthEntries:
    """Запись способа входа для файла настроек: ключи файла, открытые
    секреты и обратный разбор в тот же тип."""

    ENTRIES: ClassVar[TypeAdapter[McpAuth]] = TypeAdapter(McpAuth)

    def _entries(self) -> list[McpAuthEntry]:
        issuer = HttpLocation(scheme="http", host="127.0.0.1", port=8080, path="/")

        return [
            NoAuth(),
            BearerAuth(token=SecretStr("t0ken")),
            LocalAuth(login="agent", password=SecretStr("pw")),
            ProxyAuth(
                issuer=issuer,
                client_id="chat",
                client_secret=SecretStr("cs"),
                secret=SecretStr("ps"),
                login="ivanov",
                roles=("ADM", "dev"),
            ),
        ]

    async def test_revealed_entry_reads_back_as_the_same_entry(self) -> None:
        for entry in self._entries():
            document = entry.revealed()

            assert self.ENTRIES.validate_python(document) == entry, document

    async def test_proxy_entry_is_written_with_the_keys_of_the_settings_file(
        self,
    ) -> None:
        proxy = self._entries()[3]

        document = proxy.revealed()

        assert document["clientId"] == "chat", document
        assert document["clientSecret"] == "cs", document
        assert document["secret"] == "ps", document
        assert sorted(proxy.secret_keys()) == ["clientSecret", "secret"]

    async def test_shared_entries_stay_themselves_for_any_user(self) -> None:
        caller = McpCaller(login="ivanov", roles=frozenset({"dev"}))
        for entry in self._entries()[:3]:
            assert entry.on_behalf_of(caller) is entry, entry

    async def test_secrets_stay_masked_in_an_ordinary_dump(self) -> None:
        local = self._entries()[2]

        assert "pw" not in str(local.model_dump(mode="json")), local
        assert local.secret_keys() == ["password"]
