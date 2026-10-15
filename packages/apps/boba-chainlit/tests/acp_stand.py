"""Стенд клиента ACP: фейковая модель в цикле теста, процесс `boba acp` по
файлу настроек, запоминающий приёмник потока и подключение истории агента
ролью с паролем из [site] стенда."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

import uvicorn
from acp.experimental.v2 import schema
from chainlit_stand import NoConnectionStore
from pydantic import SecretStr

from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.chainlit.agent.client import AgentCommand, SessionUpdate, SessionUpdates
from boba.chainlit.agent.config import (
    AgentCommandConfig,
    AgentConfig,
    ChatMcpConfig,
    HookConfig,
    ModelLimitsConfig,
    PostgresAgentHistory,
)
from boba.chainlit.agent.relay import McpRelay
from boba.chainlit.agent.server import ChatCallers, HookSealing, ProcessKeys
from boba.chainlit.agent.settings import (
    AgentModel,
    AgentProvider,
    ChatAgentSettings,
    ProviderKindName,
)
from boba.connection_broker.user_connections import ArmedConnections
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.connection.auth import PasswordAuth
from boba.identity.context import CallContexts
from boba.mcp_client.client import McpServers
from boba.stand.names import StandNames
from boba.stand.site import Stand
from boba.stand.ui.fake_llm import FakeLlmApp
from boba.stand.ui.stand import free_port

API_KEY = "sk-chainlit-acp-test-secret"
MODEL_NAME = "stand-model"
AGENT_MODULE = "boba.cli.app"


class FakeModel:
    """FakeLlmApp под uvicorn в цикле теста."""

    def __init__(self) -> None:
        self.app = FakeLlmApp(token_delay_sec=0.0)
        self.port = free_port()
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.app.asgi(),
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                timeout_graceful_shutdown=1,
            )
        )
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            await asyncio.sleep(0.02)

    async def stop(self) -> None:
        self._server.should_exit = True
        if self._task is not None:
            await self._task

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def chat_settings(
        self, system_prompt: str = "You are the stand agent"
    ) -> ChatAgentSettings:
        return ChatAgentSettings(
            model_name=MODEL_NAME,
            model=AgentModel(
                provider=AgentProvider(
                    kind=ProviderKindName.OPENAI,
                    base_url=self.base_url(),
                    api_key=SecretStr(API_KEY),
                ),
                model="fake-model",
                sampling={},
                context_window=32000,
                max_output_tokens=4096,
            ),
            system_prompt=system_prompt,
        )


class AgentHistoryStand:
    """История агента в Postgres стенда: роль с паролем из [site], своя схема
    на набор и процесс xdist."""

    SCHEMA: ClassVar[str] = "acp_history"

    def __init__(self, stand: Stand) -> None:
        self._stand = stand

    def postgres(self) -> PostgresConfig:
        if not self._stand.pg_probe_user:
            msg = (
                "[site].pg_probe_user is empty: the agent history needs a password role"
            )
            raise RuntimeError(msg)

        return PostgresConfig.model_validate(
            {
                "host": self._stand.pg_host,
                "hostaddr": self._stand.pg_addr,
                "port": self._stand.pg_port,
                "dbname": self._stand.pg_probe_database,
                "connect_timeout": 10,
                "auth": PasswordAuth(
                    method="password",
                    user=self._stand.pg_probe_user,
                    password=self._stand.pg_probe_password,
                ),
            }
        )

    def schema(self) -> str:
        return StandNames().of(self.SCHEMA)

    def history(self) -> PostgresAgentHistory:
        return PostgresAgentHistory.model_validate(
            {"kind": "postgres", "postgres": self.postgres(), "schema": self.schema()}
        )

    def document(self) -> dict[str, Any]:
        """Секция [agent.history] для стенда приложения."""
        postgres = self.postgres()
        auth = postgres.auth
        assert isinstance(auth, PasswordAuth)

        return {
            "kind": "postgres",
            "schema": self.schema(),
            "postgres": {
                "host": postgres.host,
                "hostaddr": postgres.hostaddr,
                "port": postgres.port,
                "dbname": postgres.dbname,
                "connect_timeout": postgres.connect_timeout,
                "auth": {
                    "method": "password",
                    "user": auth.user,
                    "password": auth.password.get_secret_value(),
                },
            },
        }


class AgentStand:
    """Конфиг [agent] под тест: модуль агента интерпретатором теста."""

    def __init__(self, private_dir: Path, history: PostgresAgentHistory) -> None:
        self._private_dir = private_dir
        self._history = history

    def command(self) -> AgentCommand:
        return AgentCommand(
            executable=sys.executable,
            args=("-m", AGENT_MODULE, "acp", "--setting-sources", ""),
            env={},
            start_timeout_sec=60.0,
            stop_timeout_sec=10.0,
        )

    def relay(self, keys: ProcessKeys, servers: McpServers) -> McpRelay:
        """Ретранслятор mcp/message процессов теста над серверами servers;
        соединений пользователя у стенда нет."""
        contexts = CallContexts()
        credentials = KerberosCredentialSource(None, NoRefresh())
        connections = ArmedConnections(
            NoConnectionStore(), lambda: credentials, contexts
        )
        callers = ChatCallers(keys)
        sealing = HookSealing(callers, connections, contexts, timedelta(minutes=2))
        config = self.config()

        return McpRelay(
            keys, callers, servers, sealing, config.chat_mcp, config.public_url
        )

    def config(self, public_url: str = "http://127.0.0.1:1/chat") -> AgentConfig:
        return AgentConfig(
            process=AgentCommandConfig(
                command=sys.executable,
                args=["-m", AGENT_MODULE, "acp", "--setting-sources", ""],
                env={},
                private_dir=str(self._private_dir),
                start_timeout_sec=60.0,
                stop_timeout_sec=10.0,
            ),
            history=self._history,
            public_url=public_url,
            chat_mcp=ChatMcpConfig(
                enable=False,
                name="chat",
                path="/chat/mcp",
                connect_timeout_sec=10.0,
                call_timeout_sec=60.0,
            ),
            hook=HookConfig(
                enable=True, path="/chat/hooks/pre-tool-use", timeout_sec=5
            ),
            limits=ModelLimitsConfig(context_window=32000, max_output_tokens=4096),
        )


class RecordingUpdates(SessionUpdates):
    """Приёмник потока для тестов транспорта: копит уведомления и итоги."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, SessionUpdate]] = []
        self.finished: list[tuple[str, schema.IdleSessionStateUpdate]] = []
        self.abandoned: list[str] = []
        self.permissions: list[str] = []
        self.first_update = asyncio.Event()

    async def update(self, session_id: str, update: SessionUpdate) -> None:
        self.updates.append((session_id, update))
        self.first_update.set()

    async def permission(
        self, session_id: str, title: str, options: Sequence[schema.PermissionOption]
    ) -> schema.RequestPermissionResponse:
        self.permissions.append(title)
        chosen = schema.SelectedPermissionOutcomeVariant(option_id=options[0].option_id)

        return schema.RequestPermissionResponse(outcome=chosen)

    async def turn_finished(
        self, session_id: str, idle: schema.IdleSessionStateUpdate
    ) -> None:
        self.finished.append((session_id, idle))

    async def abandon(self, session_id: str) -> None:
        self.abandoned.append(session_id)

    def texts(self) -> list[str]:
        found: list[str] = []
        for _, update in self.updates:
            if not isinstance(update, schema.ContentChunk):
                continue

            content = update.content
            if isinstance(content, schema.TextContentBlock):
                found.append(content.text)

        return found


class ProcFiles:
    """Что видно о процессе снаружи: командная строка и окружение из /proc."""

    def __init__(self, pid: int) -> None:
        self._root = Path("/proc") / str(pid)

    def cmdline(self) -> list[str]:
        raw = (self._root / "cmdline").read_bytes()

        return [part.decode("utf-8") for part in raw.split(b"\0") if part]

    def environ(self) -> dict[str, str]:
        raw = (self._root / "environ").read_bytes()
        found: dict[str, str] = {}
        for part in raw.split(b"\0"):
            if not part:
                continue

            name, _, value = part.decode("utf-8", errors="replace").partition("=")
            found[name] = value

        return found
