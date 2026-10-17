"""Стенд клиента ACP: фейковая модель в цикле теста, процессы `boba acp` по
файлу настроек с ретранслятором стенда, запоминающий приёмник потока,
обычный сервер MCP по stdio и подключение истории агента ролью с паролем из
[site] стенда."""

from __future__ import annotations

import asyncio
import shlex
import sys
from collections.abc import AsyncIterator, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import pytest
import uvicorn
from acp.experimental.v2 import schema
from aid_stand import NoConnectionStore, RelayStand
from pydantic import SecretStr

from boba.agent.profile import ReasoningReturn
from boba.agent.settings import ModelSettings, ProviderKind, ProviderSettings
from boba.aid.agent.chat import (
    AskedQuestions,
    Question,
    StartedTurn,
    TurnRenderer,
)
from boba.aid.agent.client import (
    AgentClient,
    AgentCommand,
    AgentProcess,
)
from boba.aid.agent.config import (
    AgentCommandConfig,
    AgentConfig,
    AgentModelConfig,
    PostgresAgentHistory,
)
from boba.aid.agent.settings import (
    AgentWorkdirs,
    ChatAgentSettings,
    SettingsFileBuilder,
)
from boba.aid.rendering.acp_view import SessionUpdate
from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.connection.auth import PasswordAuth
from boba.identity.context import CallContexts
from boba.mcp_client.client import (
    McpServerConfig,
    McpServers,
    McpServersConfig,
    StdioCommand,
)
from boba.stand.names import StandNames
from boba.stand.site import Stand
from boba.stand.ui.fake_llm import FakeLlmApp
from boba.stand.ui.stand import REPO_ROOT, free_port

API_KEY = "sk-chainlit-acp-test-secret"
MODEL_NAME = "stand-model"
AGENT_MODULE = "boba.cli.app"


@dataclass(frozen=True)
class TlsFiles:
    """Ключ и сертификат PEM, которыми фейковая модель отвечает по https."""

    key: Path
    certificate: Path


class FakeModel:
    """FakeLlmApp под uvicorn в цикле теста; с tls — по https."""

    def __init__(self, tls: TlsFiles | None = None) -> None:
        self.app = FakeLlmApp(token_delay_sec=0.0)
        self.port = free_port()
        self._scheme = "http"
        keyfile: str | None = None
        certfile: str | None = None
        if tls is not None:
            self._scheme = "https"
            keyfile = str(tls.key)
            certfile = str(tls.certificate)

        self._server = uvicorn.Server(
            uvicorn.Config(
                self.app.asgi(),
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                timeout_graceful_shutdown=1,
                ssl_keyfile=keyfile,
                ssl_certfile=certfile,
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
        return f"{self._scheme}://127.0.0.1:{self.port}/v1"

    def chat_settings(
        self, system_prompt: str = "You are the stand agent"
    ) -> ChatAgentSettings:
        return ChatAgentSettings(
            model_name=MODEL_NAME,
            model=ModelSettings(
                provider=ProviderSettings(
                    kind=ProviderKind.OPENAI,
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
            {"kind": "postgres", "schema": self.schema()}
        )


class AgentStand:
    """Конфиг [agent] под тест: модуль агента интерпретатором теста."""

    def __init__(self, private_dir: Path, history: PostgresAgentHistory) -> None:
        self._private_dir = private_dir
        self._history = history

    def command(self) -> AgentCommand:
        return AgentCommand(
            executable=sys.executable,
            args=("-m", AGENT_MODULE, "acp", "--setting-sources", ""),
            start_timeout_sec=60.0,
            request_timeout_sec=60.0,
            stop_timeout_sec=10.0,
        )

    def config(self, public_url: str = "http://127.0.0.1:1/chat") -> AgentConfig:
        return AgentConfig(
            process=AgentCommandConfig(
                command=shlex.join([sys.executable, "-m", AGENT_MODULE]),
                args=["acp", "--setting-sources", ""],
                private_dir=str(self._private_dir),
                start_timeout_sec=60.0,
                request_timeout_sec=60.0,
                stop_timeout_sec=10.0,
            ),
            history=self._history,
            public_url=public_url,
            seal_ttl_sec=600,
            model=AgentModelConfig(
                context_window=32000,
                max_output_tokens=4096,
                reasoning_return=ReasoningReturn.NEVER,
            ),
        )


class RecordingUpdates(TurnRenderer):
    """Реализация TurnRenderer для тестов транспорта и агента треда: копит
    уведомления, итоги и messageId вопросов; ход начинает ответом агента на
    вопрос."""

    def __init__(self) -> None:
        self.asked: list[str] = []
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

    def stopping(self, session_id: str) -> None:
        return

    async def turn_started(
        self, question: Question, start: Coroutine[Any, Any, StartedTurn]
    ) -> StartedTurn:
        turn = await start
        self.asked.append(turn.key)

        return turn

    async def cleared(self) -> None:
        return

    async def loaded(self, session_id: str) -> None:
        return

    def texts(self) -> list[str]:
        """Тексты агента: куски живого хода и целые сообщения воспроизведения."""
        found: list[str] = []
        for _, update in self.updates:
            blocks: Sequence[object] = ()
            match update:
                case schema.ContentChunk():
                    blocks = (update.content,)
                case schema.AgentMessage() | schema.AgentThought():
                    blocks = update.content or ()

            for block in blocks:
                if isinstance(block, schema.TextContentBlock):
                    found.append(block.text)

        return found


class Agents:
    """Процессы агента теста: один файл настроек, несколько запусков; поток
    сессий идёт приёмнику теста через обёртку агента треда (AskedQuestions),
    которая решает конец хода по idle. Запросы mcp/message процессов отвечает
    ретранслятор стенда (RelayStand) над серверами servers; регистрации
    выдаёт registrations."""

    TURN_TIMEOUT_SEC: float = 60.0

    def __init__(
        self, root: Path, model: FakeModel, stand: Stand, servers: McpServersConfig
    ) -> None:
        self._root = root
        self._model = model
        history = AgentHistoryStand(stand)
        self._stand = AgentStand(root, history.history())
        self._builder = SettingsFileBuilder(
            self._stand.config(), history.postgres(), servers
        )
        self._servers = McpServers(servers)
        credentials = KerberosCredentialSource(None, NoRefresh())
        relay = RelayStand(NoConnectionStore(), lambda: credentials, CallContexts())
        self.registrations = relay.registrations
        self._relay = relay.relay(self._servers)
        self.started: list[AgentProcess] = []
        self.questions: dict[AgentProcess, AskedQuestions] = {}

    async def start(
        self,
        updates: RecordingUpdates,
        thread: str = "t",
        servers: Sequence[schema.AcpMcpServer] = (),
    ) -> AgentProcess:
        """Процесс агента с объявлениями servers в каждой сессии."""
        chat = self._model.chat_settings()
        workdir = AgentWorkdirs(self._root).create(thread)
        workdir.write(self._builder.build(chat, None, {}), chat.system_prompt)
        questions = AskedQuestions(updates)
        process = AgentProcess(
            self._stand.command(),
            workdir,
            AgentClient(questions),
            self._relay,
            servers,
            "0.0",
        )
        await process.start()
        self.started.append(process)
        self.questions[process] = questions

        return process

    async def turn(
        self, process: AgentProcess, session_id: str, text: str
    ) -> tuple[str, schema.IdleSessionStateUpdate]:
        """Вопрос в сессию до idle: messageId вопроса и idle хода."""
        ending = self.questions[process].ending(session_id)
        block = schema.TextContentBlock(text=text)
        message_id = await process.prompt(session_id, [block])
        idle = await asyncio.wait_for(ending, self.TURN_TIMEOUT_SEC)

        return message_id, idle

    async def close_all(self) -> None:
        for process in self.started:
            await process.close()

        await self._servers.stop()


@pytest.fixture
async def model() -> AsyncIterator[FakeModel]:
    """Фейковая модель теста под uvicorn в его цикле."""
    fake = FakeModel()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture(scope="session")
def standard() -> McpServerConfig:
    """Обычный сервер MCP стенда клиента MCP (standard_server.py) по stdio."""
    script = REPO_ROOT / "packages/services/boba-mcp-client/tests/standard_server.py"
    endpoint = StdioCommand(command=sys.executable, args=(str(script), "stdio"))

    return McpServerConfig(
        endpoint=endpoint, connect_timeout_sec=20.0, call_timeout_sec=60.0
    )


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
