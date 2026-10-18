"""Способы входа клиента MCP на настоящем сервере авторизации сервиса boba:
local — учёткой с паролем через страницу входа без человека, proxy —
утверждением о логине и ролях из самой записи; обновление токена по
истечении срока в обоих."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import ClassVar

import mcp.types as mt
import pytest
from pydantic import SecretStr

from boba.mcp_client.client import (
    BearerAuth,
    HttpEndpoint,
    HttpLocation,
    LocalAuth,
    McpAuthEntry,
    McpCaller,
    McpClientError,
    McpServerConfig,
    McpServers,
    McpToolServer,
    NoAuth,
    ProxyAuth,
)
from boba.stand.service_signin import FakeServiceProcess
from boba.toolkit.mcp import McpResults

pytestmark = pytest.mark.anyio

OWN = McpServers.OWN


class AuthStand(FakeServiceProcess):
    """Сервис стенда с записями способов входа клиента и портом к нему."""

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

        return McpToolServer("boba", config)

    async def whoami(self, server: McpToolServer) -> str:
        """Текст для модели из ответа fake_whoami: логин, роли и токен."""
        params = mt.CallToolRequestParams(name="fake_whoami", arguments={})
        reply = await server.call(OWN, params, None, None, None, None)

        return McpResults().result(params.name, "call-whoami", reply).llm_view()


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

        await server.described(OWN)
        text = await stand.whoami(server)

        assert text.startswith("carol|dev|"), text
        assert requests.count("GET", "/authorize") == 1, requests.lines
        assert requests.count("POST", "/login") == 1, requests.lines
        assert requests.count("POST", "/register") == 1, requests.lines

    async def test_wrong_password_is_a_client_error_with_the_server_reply(
        self, stand: AuthStand, opened: list[McpToolServer]
    ) -> None:
        server = stand.server(stand.local(AuthStand.LOGIN, "not-the-password"))
        opened.append(server)

        with pytest.raises(McpClientError) as caught:
            await server.described(OWN)

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
            await server.described(OWN)

        text = str(caught.value)
        assert "HTTP 403" in text, text
        assert "'ghost'" in text, text

    async def test_expired_token_is_renewed_by_the_refresh_token(
        self, brief: AuthStand, opened: list[McpToolServer], requests: RequestLog
    ) -> None:
        server = brief.server(brief.local(AuthStand.LOGIN, AuthStand.PASSWORD))
        opened.append(server)
        await server.described(OWN)
        first = await brief.whoami(server)
        issued = requests.count("POST", "/token")

        await asyncio.sleep(brief.token_ttl_sec + 1.5)
        second = await brief.whoami(server)

        assert first.startswith("carol|dev|"), first
        assert second.startswith("carol|dev|"), second
        assert requests.count("POST", "/token") > issued, requests.lines
        assert requests.count("POST", "/login") == 1, requests.lines


class TestProxyAuth:
    async def test_login_and_roles_of_the_entry_reach_the_server(
        self, stand: AuthStand, opened: list[McpToolServer]
    ) -> None:
        server = stand.server(stand.proxy("ivanov", ["dev"]))
        opened.append(server)

        await server.described(OWN)
        text = await stand.whoami(server)

        assert text.startswith("ivanov|dev|"), text

    async def test_entry_takes_the_user_of_the_host(
        self, stand: AuthStand, opened: list[McpToolServer]
    ) -> None:
        declared = stand.proxy("", [])
        caller = McpCaller(login="petrov", roles=frozenset({"dev", "ADM"}))
        server = stand.server(declared.on_behalf_of(caller))
        opened.append(server)

        await server.described(OWN)
        text = await stand.whoami(server)

        assert text.startswith("petrov|ADM,dev|"), text

    async def test_entry_without_a_login_is_a_client_error(
        self, stand: AuthStand, opened: list[McpToolServer]
    ) -> None:
        server = stand.server(stand.proxy("", []))
        opened.append(server)

        with pytest.raises(McpClientError) as caught:
            await server.described(OWN)

        assert "names no login" in str(caught.value), caught.value

    async def test_expired_token_is_replaced_by_a_new_exchange(
        self, brief: AuthStand, opened: list[McpToolServer], requests: RequestLog
    ) -> None:
        server = brief.server(brief.proxy("ivanov", ["dev"]))
        opened.append(server)
        await server.described(OWN)
        first = await brief.whoami(server)
        issued = requests.count("POST", "/token")

        await asyncio.sleep(brief.token_ttl_sec + 1.5)
        second = await brief.whoami(server)

        assert first.startswith("ivanov|dev|"), first
        assert second.startswith("ivanov|dev|"), second
        assert requests.count("POST", "/token") > issued, requests.lines
        assert requests.count("POST", "/login") == 0, requests.lines


class TestAuthEntries:
    """Способ входа для пользователя клиента и секреты в обычной выгрузке."""

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

    async def test_shared_entries_stay_themselves_for_any_user(self) -> None:
        caller = McpCaller(login="ivanov", roles=frozenset({"dev"}))
        for entry in self._entries()[:3]:
            assert entry.on_behalf_of(caller) is entry, entry

    async def test_secrets_stay_masked_in_an_ordinary_dump(self) -> None:
        local = self._entries()[2]

        assert "pw" not in str(local.model_dump(mode="json")), local
