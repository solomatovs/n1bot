"""Заменяемость агента: один сценарий клиента SDK (`spawn_agent_process`)
против разных агентов ACP, разница — команда запуска. `boba acp` идёт
всегда (фейковая модель, сервер MCP стенда); `claude-agent-acp` и
`hermes acp` — под меткой integration, командами из их README, и
пропускаются, когда команды нет на машине. Чужим агентам нужны их ключи и
вход в аккаунт из их собственных настроек — процесс получает окружение
пользователя целиком; сам тест ключей не знает и не создаёт. Запуск Claude
расходует аккаунт пользователя, поэтому без `-m integration` чужие агенты не
стартуют.

Сценарий и проверки — только по спецификации, без расширений агента:
`initialize` → protocolVersion 1; `session/new` с одним http-сервером MCP →
sessionId; `session/prompt` → поток `session/update` той же сессии до ответа,
среди него `agent_message_chunk`, итог `end_turn`; `session/cancel` посреди
хода → `cancelled` и ни одного `session/update` после ответа;
`session/load`, если агент объявил `loadSession`, → воспроизведение беседы
(`user_message_chunk` с текстом вопроса, `agent_message_chunk`) до ответа.
Текст, который держит ход для отмены, у агентов разный: фейковой модели
нужен маркер вызова инструмента, настоящей — долгая задача."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest
from acp import spawn_agent_process, text_block
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    ClientCapabilities,
    Implementation,
    TextContentBlock,
    UserMessageChunk,
)
from test_cli_acp import (
    READ_TIMEOUT_SEC,
    AcpProcess,
    FakeModel,
    Frames,
    McpStand,
    RecordingClient,
    call,
)

from boba.cli.acp import AcpMethod, StopReasonName

pytestmark = pytest.mark.anyio

TURN_CEILING_SEC = 180.0


class AgentName(StrEnum):
    """Агенты, против которых идёт сценарий."""

    BOBA = "boba"
    CLAUDE = "claude-agent-acp"
    HERMES = "hermes"


@dataclass(frozen=True)
class AgentUnderTest:
    """Команда запуска агента, вопрос, который держит ход, и окружение
    процесса: None — окружение пользователя целиком."""

    name: AgentName
    command: Sequence[str]
    busy_prompt: str
    environment: Mapping[str, str] | None


class ForeignAgents:
    """Чужие агенты командами из их README; без исполняемого файла на
    машине сценарий для них пропускается."""

    COMMANDS: Mapping[AgentName, Sequence[str]] = {
        AgentName.CLAUDE: ("npx", "@agentclientprotocol/claude-agent-acp"),
        AgentName.HERMES: ("hermes", "acp"),
    }
    BUSY_PROMPT = (
        "Count from 1 to 3000 out loud, one number per line, "
        "and do not stop until you reach 3000."
    )

    def agent(self, name: AgentName) -> AgentUnderTest:
        command = self.COMMANDS[name]
        if shutil.which(command[0]) is None:
            pytest.skip(f"{name.value}: {command[0]} is not installed on this machine")

        return AgentUnderTest(name, command, self.BUSY_PROMPT, None)


class BobaAgent:
    """`boba acp` над фейковой моделью, которая отвечает на любой текст;
    ход держит вызов `sleep_for` сервера стенда."""

    BUSY_SECONDS = 30

    def __init__(self, root: Path, model: FakeModel) -> None:
        self._process = AcpProcess(root, model)
        self._root = root

    def agent(self) -> AgentUnderTest:
        self._process.write_settings(self._process.settings_document())
        (self._root / "home").mkdir(exist_ok=True)
        command = (
            sys.executable,
            "-m",
            AcpProcess.MODULE,
            "acp",
            "--settings",
            str(self._process.settings_path()),
            "--setting-sources",
            "user",
        )
        busy = call({"name": "sleep_for", "arguments": {"seconds": self.BUSY_SECONDS}})
        environment = {"HOME": str(self._root / "home"), "PATH": os.environ["PATH"]}

        return AgentUnderTest(AgentName.BOBA, command, busy, environment)


class ParityScenario:
    """Сценарий спецификации против одного агента; кадры — в наблюдателе,
    уведомления — в клиенте."""

    GREETING = "Say hello in one short sentence."

    def __init__(self, agent: AgentUnderTest, mcp: McpStand, cwd: Path) -> None:
        self._agent = agent
        self._mcp = mcp
        self._cwd = cwd
        self.client = RecordingClient()
        self.frames = Frames()

    async def run(self) -> None:
        async with spawn_agent_process(
            self.client,
            *self._agent.command,
            env=self._agent.environment,
            cwd=str(self._cwd),
            observers=[self.frames],
        ) as (conn, _process):
            initialized = await conn.initialize(
                protocol_version=1,
                client_capabilities=ClientCapabilities(),
                client_info=Implementation(name="boba-parity", version="0"),
            )
            assert initialized.protocol_version == 1

            session = await conn.new_session(
                cwd=str(self._cwd), mcp_servers=[self._mcp.server()]
            )
            session_id = session.session_id
            assert session_id

            await self._greeting_turn(conn, session_id)
            await self._cancelled_turn(conn, session_id)
            if self._loads(initialized.agent_capabilities):
                await self._load(conn, session_id)

    @staticmethod
    def _loads(capabilities: AgentCapabilities | None) -> bool:
        if capabilities is None:
            return False

        return bool(capabilities.load_session)

    async def _greeting_turn(self, conn: Any, session_id: str) -> None:
        response = await asyncio.wait_for(
            conn.prompt(session_id=session_id, prompt=[text_block(self.GREETING)]),
            TURN_CEILING_SEC,
        )

        assert response.stop_reason == StopReasonName.END_TURN.value
        assert self.client.of(AgentMessageChunk)
        for seen, _update in self.client.updates:
            assert seen == session_id

        self._assert_nothing_after_the_response(AcpMethod.SESSION_PROMPT, 0)

    async def _cancelled_turn(self, conn: Any, session_id: str) -> None:
        seen_before = len(self.client.updates)
        prompting = asyncio.create_task(
            conn.prompt(
                session_id=session_id, prompt=[text_block(self._agent.busy_prompt)]
            )
        )
        await self._wait_for_an_update(seen_before)
        await conn.cancel(session_id=session_id)
        response = await asyncio.wait_for(prompting, TURN_CEILING_SEC)

        assert response.stop_reason == StopReasonName.CANCELLED.value
        self._assert_nothing_after_the_response(AcpMethod.SESSION_PROMPT, 1)

    async def _load(self, conn: Any, session_id: str) -> None:
        seen_before = len(self.client.updates)
        await asyncio.wait_for(
            conn.load_session(
                session_id=session_id,
                cwd=str(self._cwd),
                mcp_servers=[self._mcp.server()],
            ),
            TURN_CEILING_SEC,
        )
        replayed = self.client.updates[seen_before:]

        user_text = "".join(self._texts(replayed, UserMessageChunk))
        assert self.GREETING in user_text
        assert "".join(self._texts(replayed, AgentMessageChunk))
        self._assert_nothing_after_the_response(AcpMethod.SESSION_LOAD, 0)

    async def _wait_for_an_update(self, seen_before: int) -> None:
        deadline = asyncio.get_running_loop().time() + READ_TIMEOUT_SEC
        while len(self.client.updates) <= seen_before:
            if asyncio.get_running_loop().time() > deadline:
                msg = "the agent sent no session/update for the busy prompt"
                raise AssertionError(msg)

            await asyncio.sleep(0.05)

    def _assert_nothing_after_the_response(self, method: AcpMethod, index: int) -> None:
        request_id = self.frames.request_id(method.value, index)
        answered_at = self.frames.position_of_response(request_id)
        for message in self.frames.incoming_after(answered_at):
            assert message.get("method") != AcpMethod.SESSION_UPDATE.value, message

    @staticmethod
    def _texts(updates: Sequence[tuple[str, Any]], kind: type) -> list[str]:
        found: list[str] = []
        for _seen, update in updates:
            if not isinstance(update, kind):
                continue

            content = update.content
            if isinstance(content, TextContentBlock):
                found.append(content.text)

        return found


@pytest.fixture(
    params=[
        pytest.param(AgentName.BOBA, id=AgentName.BOBA.value),
        pytest.param(
            AgentName.CLAUDE,
            id=AgentName.CLAUDE.value,
            marks=pytest.mark.integration,
        ),
        pytest.param(
            AgentName.HERMES,
            id=AgentName.HERMES.value,
            marks=pytest.mark.integration,
        ),
    ]
)
def agent(
    request: pytest.FixtureRequest, answering_model: FakeModel, tmp_path: Path
) -> AgentUnderTest:
    name = request.param
    if name is AgentName.BOBA:
        return BobaAgent(tmp_path, answering_model).agent()

    return ForeignAgents().agent(name)


class TestParity:
    async def test_the_spec_scenario_passes(
        self, agent: AgentUnderTest, mcp: McpStand, tmp_path: Path
    ) -> None:
        cwd = tmp_path / "project"
        cwd.mkdir()

        await ParityScenario(agent, mcp, cwd).run()
