"""Стенд сервиса boba-mcp над фейками отдельным процессом и вход в него от
имени пользователя.

Ошибки:
ServiceSignInError — сервис не начал слушать за срок ожидания либо отказал
    в обмене утверждения на токен.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import ClassVar

import httpx

from boba.auth.proxy import ProxyAssertions

__all__ = ["FakeServiceProcess", "ServiceSignIn", "ServiceSignInError"]


class ServiceSignInError(Exception):
    """Стенд сервиса не выдал токен доступа."""


class ServiceSignIn:
    """Токен доступа endpoint'а стенда сервиса boba-mcp для пользователя.

    Создаётся тестом или стендом, которому нужен вызов сервиса от имени
    названного пользователя. Вход — тот же, что у чата: утверждение о
    пользователе подписывается ключом proxy стенда и меняется на /token
    сервиса на токен (grant jwt-bearer). Токен годен только на endpoint'е,
    чей адрес назван ресурсом. Запрос блокирующий: из цикла событий, в
    котором слушает сам сервис, его зовут через asyncio.to_thread.
    """

    GRANT: ClassVar[str] = "urn:ietf:params:oauth:grant-type:jwt-bearer"
    WAIT_SEC: ClassVar[float] = 30.0
    POLL_SEC: ClassVar[float] = 0.2

    def __init__(
        self, public_url: str, client_id: str, client_secret: str, proxy_secret: str
    ) -> None:
        self._public_url = public_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._assertions = ProxyAssertions(proxy_secret, 0)

    def token(self, login: str, roles: str, resource: str) -> str:
        """Токен пользователя login с ролями roles для endpoint'а resource."""
        form = {
            "grant_type": self.GRANT,
            "assertion": self._assertions.issue(
                self._client_id, login, roles, self._public_url, resource
            ),
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "resource": resource,
        }
        reply = self._posted(form)
        if reply.status_code != httpx.codes.OK:
            msg = (
                f"POST {self._public_url}/token for {login!r} with roles {roles!r} "
                f"and resource {resource}: expected 200 with a token, got "
                f"{reply.status_code}: {reply.text[:300]}"
            )
            raise ServiceSignInError(msg)

        return str(reply.json()["access_token"])

    def _posted(self, form: dict[str, str]) -> httpx.Response:
        """Ответ /token; сервис стенда может ещё подниматься — ждём порт."""
        deadline = time.monotonic() + self.WAIT_SEC
        while True:
            try:
                return httpx.post(f"{self._public_url}/token", data=form)
            except httpx.ConnectError as exc:
                if time.monotonic() >= deadline:
                    msg = (
                        f"POST {self._public_url}/token: the service did not "
                        f"listen within {self.WAIT_SEC}s: {exc}"
                    )
                    raise ServiceSignInError(msg) from exc

            time.sleep(self.POLL_SEC)


class FakeServiceProcess:
    """Сервис boba-mcp над фейками стенда (`boba-mcp/tests/service_stand.py`)
    отдельным процессом с названным сроком токена доступа.

    Создаётся тестами клиента MCP и агента: клиент видит сервис только по
    сети, как в приложении. Входы стенда — local (учётка LOGIN с ролью dev,
    учётка ROLELESS без ролей) и proxy (клиент CLIENT_ID под ключом
    PROXY_SECRET); endpoint ENDPOINT отдаёт все инструменты стенда.
    """

    STAND: ClassVar[Path] = (
        Path(__file__).resolve().parents[5]
        / "apps"
        / "boba-mcp"
        / "tests"
        / "service_stand.py"
    )
    HOST: ClassVar[str] = "127.0.0.1"
    ENDPOINT: ClassVar[str] = "/mcp/service"
    METADATA: ClassVar[str] = "/.well-known/oauth-authorization-server"
    WAIT_SEC: ClassVar[float] = 60.0
    POLL_SEC: ClassVar[float] = 0.2
    STOP_SEC: ClassVar[float] = 10.0

    LOGIN: ClassVar[str] = "carol"
    PASSWORD: ClassVar[str] = "carol-pw"  # noqa: S105 — учётка стенда
    ROLELESS: ClassVar[str] = "ghost"
    ROLELESS_PASSWORD: ClassVar[str] = "ghost-pw"  # noqa: S105 — учётка стенда
    CLIENT_ID: ClassVar[str] = "stand-chat"
    CLIENT_SECRET: ClassVar[str] = "stand-client-secret"  # noqa: S105 — ключ стенда
    PROXY_SECRET: ClassVar[str] = "stand-proxy-secret"  # noqa: S105 — ключ стенда

    def __init__(self, workdir: Path, token_ttl_sec: int) -> None:
        with socket.socket() as probe:
            probe.bind((self.HOST, 0))
            self.port: int = probe.getsockname()[1]

        self.token_ttl_sec = token_ttl_sec
        self._process = subprocess.Popen(  # noqa: S603 — стенд теста
            [
                sys.executable,
                str(self.STAND),
                str(self.port),
                str(workdir),
                str(token_ttl_sec),
            ]
        )

    def public_url(self) -> str:
        return f"http://{self.HOST}:{self.port}"

    def url(self) -> str:
        """Адрес endpoint'а MCP стенда."""
        return f"{self.public_url()}{self.ENDPOINT}"

    def wait(self) -> None:
        """Ждёт, пока сервер авторизации стенда ответит метаданными.

        Ошибки:
        ServiceSignInError — стенд не начал отвечать за срок ожидания.
        """
        url = f"{self.public_url()}{self.METADATA}"
        deadline = time.monotonic() + self.WAIT_SEC
        while time.monotonic() < deadline:
            if self._answers(url):
                return

            time.sleep(self.POLL_SEC)

        msg = f"GET {url}: the stand service did not answer within {self.WAIT_SEC}s"
        raise ServiceSignInError(msg)

    def stop(self) -> None:
        self._process.terminate()
        self._process.wait(timeout=self.STOP_SEC)

    @staticmethod
    def _answers(url: str) -> bool:
        try:
            return httpx.get(url).status_code == httpx.codes.OK
        except httpx.TransportError:
            return False
