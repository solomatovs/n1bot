"""Стенд службы агента (`boba serve`) в цикле событий теста и вход в неё
proxy доверенного клиента.

Ошибки:
ServiceSignInError — служба не выдала токен (boba.stand.service_signin).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

import uvicorn

from boba.cli.serve import ServeConfig, ServeHistory, ServeHost
from boba.krb import KerberosWorkspaceConfig
from boba.stand.service_signin import ServiceSignIn
from boba.stand.ui.stand import StandPorts

__all__ = ["AgentServiceStand", "ServiceProfiles"]


class ServiceProfiles:
    """Профили агента стенда [agent.profiles]: роли и профиль модели над
    фейковой моделью по адресу base_url."""

    API_KEY: ClassVar[str] = "sk-agent-service-stand"

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url
        self._profiles: dict[str, dict[str, Any]] = {}

    def add(self, name: str, roles: Sequence[str], model: str) -> ServiceProfiles:
        self._profiles[name] = {
            "roles": list(roles),
            "model": {
                "provider": {
                    "kind": "openai",
                    "baseUrl": self._base_url,
                    "apiKey": self.API_KEY,
                },
                "model": model,
                "contextWindow": 32000,
                "maxOutputTokens": 4096,
            },
        }

        return self

    def section(self) -> dict[str, dict[str, Any]]:
        return dict(self._profiles)


class AgentServiceStand:
    """Служба агента на свободном порту в цикле теста: сессии — потоки этого
    же процесса, вход — proxy клиента CLIENT_ID под ключом PROXY_SECRET.
    history — подключение истории (своя схема стенда), settings — слой
    настроек агента без истории и моделей (права, сессии), profiles —
    [agent.profiles], idle_sec — простой, после которого служба закрывает
    брошенное подключение HTTP."""

    CLIENT_ID: ClassVar[str] = "stand-chat"
    CLIENT_SECRET: ClassVar[str] = "stand-agent-client-secret"  # noqa: S105 — ключ стенда
    PROXY_SECRET: ClassVar[str] = "stand-agent-proxy-secret"  # noqa: S105 — ключ стенда
    SESSION_SECRET: ClassVar[str] = "stand-agent-session-secret"  # noqa: S105 — ключ стенда
    PATH: ClassVar[str] = "/acp"
    VERSION: ClassVar[str] = "0.0.0-stand"
    WAIT_SEC: ClassVar[float] = 60.0

    def __init__(
        self,
        krb: KerberosWorkspaceConfig,
        history: ServeHistory,
        settings: Mapping[str, Any],
        profiles: ServiceProfiles,
        idle_sec: float,
    ) -> None:
        self.port = StandPorts().free()
        self.public_url = f"http://127.0.0.1:{self.port}"
        config = ServeConfig.model_validate(
            {
                "schema": "agent_service_stand",
                "krb": krb.model_dump(),
                "auth": [
                    {
                        "type": "proxy",
                        "secret": self.PROXY_SECRET,
                        "roles": {"header": {}},
                    }
                ],
                "session": {
                    "auth_secret": self.SESSION_SECRET,
                    "session_ttl_sec": 300,
                    "session_max_sec": 3600,
                    "generation": "stand",
                },
                "agent": {
                    "host": "127.0.0.1",
                    "port": self.port,
                    "path": self.PATH,
                    "public_url": self.public_url,
                    "shutdown_timeout_sec": 2,
                    "idle_sec": idle_sec,
                    "history": history,
                    "settings": dict(settings),
                    "profiles": profiles.section(),
                    "clients": {self.CLIENT_ID: {"secret": self.CLIENT_SECRET}},
                },
            }
        )
        self.host = ServeHost(config, self.VERSION)
        self._sign_in = ServiceSignIn(
            self.public_url, self.CLIENT_ID, self.CLIENT_SECRET, self.PROXY_SECRET
        )
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    def url(self) -> str:
        return f"{self.public_url}{self.PATH}"

    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}{self.PATH}"

    async def start(self) -> None:
        await self.host.start()
        server = uvicorn.Server(
            uvicorn.Config(
                self.host.app(),
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                timeout_graceful_shutdown=2,
            )
        )
        self._server = server
        task = asyncio.create_task(server.serve())
        self._task = task
        while not server.started:
            if task.done():
                await task

            await asyncio.sleep(0.02)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True

        if self._task is not None:
            await self._task

        await self.host.stop()

    async def token(self, login: str, roles: str) -> str:
        """Токен ресурса ACP по утверждению доверенного клиента."""
        return await asyncio.to_thread(self._sign_in.token, login, roles, self.url())

    async def sessions_drop_to(self, wanted: int) -> None:
        """Ждёт, пока у службы останется wanted открытых сессий."""
        deadline = time.monotonic() + self.WAIT_SEC
        while True:
            opened = self.host.sessions()
            if opened == wanted:
                return

            if time.monotonic() >= deadline:
                msg = (
                    f"agent service stand: expected {wanted} open sessions "
                    f"within {self.WAIT_SEC}s, got {opened}"
                )
                raise AssertionError(msg)

            await asyncio.sleep(0.1)
