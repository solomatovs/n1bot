"""Стенд службы агента (boba-agent-service) в цикле событий теста и вход в
неё proxy доверенного клиента.

Ошибки:
ServiceSignInError — служба не выдала токен (boba.stand.service_signin).
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

import uvicorn

from boba.agent_service.app import AgentServiceConfig, AgentServiceHost
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
    """Служба агента на свободном порту в цикле теста: процессы агента —
    модуль boba.cli.app интерпретатором теста, вход — proxy клиента
    CLIENT_ID под ключом PROXY_SECRET. settings — базовый слой файла
    настроек (история, окружение, права), profiles — [agent.profiles]."""

    CLIENT_ID: ClassVar[str] = "stand-chat"
    CLIENT_SECRET: ClassVar[str] = "stand-agent-client-secret"  # noqa: S105 — ключ стенда
    PROXY_SECRET: ClassVar[str] = "stand-agent-proxy-secret"  # noqa: S105 — ключ стенда
    SESSION_SECRET: ClassVar[str] = "stand-agent-session-secret"  # noqa: S105 — ключ стенда
    PATH: ClassVar[str] = "/acp"
    AGENT_MODULE: ClassVar[str] = "boba.cli.app"
    WAIT_SEC: ClassVar[float] = 60.0

    def __init__(  # noqa: PLR0913 — стенд собирается всеми своими частями
        self,
        krb: KerberosWorkspaceConfig,
        workdir: Path,
        settings: Mapping[str, Any],
        profiles: ServiceProfiles,
        max_processes: int,
        idle_sec: float,
    ) -> None:
        self.port = StandPorts().free()
        self.public_url = f"http://127.0.0.1:{self.port}"
        config = AgentServiceConfig.model_validate(
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
                    "process": {
                        "command": sys.executable,
                        "args": [
                            "-m",
                            self.AGENT_MODULE,
                            "acp",
                            "--setting-sources",
                            "",
                        ],
                        "workdir": str(workdir),
                        "stop_timeout_sec": 5,
                        "max_processes": max_processes,
                        "idle_sec": idle_sec,
                    },
                    "settings": dict(settings),
                    "profiles": profiles.section(),
                    "clients": {self.CLIENT_ID: {"secret": self.CLIENT_SECRET}},
                },
            }
        )
        self.host = AgentServiceHost(config)
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

    async def processes_drop_to(self, wanted: int) -> None:
        """Ждёт, пока у службы останется wanted процессов агента."""
        deadline = time.monotonic() + self.WAIT_SEC
        while True:
            taken = self.host.quota().taken()
            if taken == wanted:
                return

            if time.monotonic() >= deadline:
                msg = (
                    f"agent service stand: expected {wanted} agent processes "
                    f"within {self.WAIT_SEC}s, got {taken}"
                )
                raise AssertionError(msg)

            await asyncio.sleep(0.1)
