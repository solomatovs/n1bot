"""Транспорт и жизненный цикл клиента ACP v2 против настоящего `boba acp` с
FakeLlmApp и историей в Postgres стенда: ход, который кончается idle, отмена
идущего хода, закрытие процесса; история — `session/resume` с
воспроизведением под messageId вопросов, `session/close`, `session/delete`,
ветка `session/fork` по messageId."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from uuid import UUID

import pytest
from acp.experimental.v2 import schema
from acp_stand import AgentHistoryStand, AgentStand, FakeModel, RecordingUpdates

from boba.chainlit.agent.chat import AskedQuestions
from boba.chainlit.agent.client import AgentClient, AgentProcess, AgentRequestError
from boba.chainlit.agent.server import ProcessKeys
from boba.chainlit.agent.settings import AgentWorkdirs, SettingsFileBuilder
from boba.mcp_client.client import McpServers, McpServersConfig
from boba.stand.site import Stand
from boba.stand.ui.fake_llm import ScenarioName
from boba.transport.acp.protocol import AcpErrorCode

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Контекст chainlit этим тестам не нужен: они живут ниже обработчиков."""


@pytest.fixture
async def model() -> AsyncIterator[FakeModel]:
    fake = FakeModel()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


class Agents:
    """Процессы агента теста: один файл настроек, несколько запусков; поток
    сессий идёт приёмнику теста через обёртку агента треда (AskedQuestions),
    которая решает конец хода по idle. Запросы mcp/message процессов отвечает
    ретранслятор над серверами servers; регистрации выдаёт keys."""

    TURN_TIMEOUT_SEC: float = 60.0

    def __init__(
        self, root: Path, model: FakeModel, stand: Stand, servers: McpServersConfig
    ) -> None:
        self._root = root
        self._model = model
        self._stand = AgentStand(root, AgentHistoryStand(stand).history())
        self._builder = SettingsFileBuilder(self._stand.config())
        self._servers = McpServers(servers)
        self.keys = ProcessKeys()
        self._relay = self._stand.relay(self.keys, self._servers)
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
        workdir.write(self._builder.build(chat), chat.system_prompt)
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
        message_id = await process.prompt(session_id, [_text(text)])
        idle = await asyncio.wait_for(ending, self.TURN_TIMEOUT_SEC)

        return message_id, idle

    async def close_all(self) -> None:
        for process in self.started:
            await process.close()

        await self._servers.stop()


@pytest.fixture
async def agents(
    tmp_path: Path, model: FakeModel, stand: Stand
) -> AsyncIterator[Agents]:
    made = Agents(tmp_path / "agents", model, stand, McpServersConfig(servers={}))
    try:
        yield made
    finally:
        await made.close_all()


def prompt_text(scenario: ScenarioName) -> str:
    return f"please {scenario.value}"


class TestTurn:
    """Сессия и ход: ответ prompt — messageId вопроса, конец хода — idle."""

    async def test_prompt_answers_a_uuid_and_idle_ends_the_turn(
        self, agents: Agents
    ) -> None:
        updates = RecordingUpdates()
        process = await agents.start(updates)
        opened = await process.open_session()
        text = prompt_text(ScenarioName.THINKING_ANSWER)

        message_id, idle = await agents.turn(process, opened.session_id, text)

        assert UUID(message_id)
        assert idle.stop_reason == "end_turn"
        joined = "".join(updates.texts())
        assert "answer the question" in joined
        assert "reason about the question" in joined
        assert all(sid == opened.session_id for sid, _ in updates.updates)

    async def test_tool_call_of_an_unknown_tool_is_reported_as_failed(
        self, agents: Agents
    ) -> None:
        updates = RecordingUpdates()
        process = await agents.start(updates)
        opened = await process.open_session()
        spec = {"name": "no_such_tool", "arguments": {"x": 1}}
        text = f"{ScenarioName.CALL.value} {json.dumps(spec)}"

        _, idle = await agents.turn(process, opened.session_id, text)

        calls: list[schema.SessionToolCallUpdate] = []
        for _, update in updates.updates:
            if isinstance(update, schema.SessionToolCallUpdate):
                calls.append(update)

        assert idle.stop_reason == "end_turn"
        assert calls, "tool_call_update did not arrive"
        assert any(call.status == "failed" for call in calls)
        assert not updates.permissions, "bypassPermissions must not ask"

    async def test_cancel_stops_the_turn_with_cancelled(self, agents: Agents) -> None:
        updates = RecordingUpdates()
        process = await agents.start(updates)
        opened = await process.open_session()
        session_id = opened.session_id
        spec = {"name": "no_such_tool", "arguments": {}}
        text = f"{ScenarioName.LOOP.value} {json.dumps(spec)}"

        ending = agents.questions[process].ending(session_id)
        await process.prompt(session_id, [_text(text)])
        await asyncio.wait_for(updates.first_update.wait(), 30)
        await process.cancel(session_id)
        idle = await asyncio.wait_for(ending, 30)

        assert idle.stop_reason == "cancelled"

    async def test_unknown_session_is_refused(self, agents: Agents) -> None:
        process = await agents.start(RecordingUpdates())
        with pytest.raises(AgentRequestError) as refused:
            await process.prompt("no-such-session", [_text("hi")])

        assert refused.value.code == AcpErrorCode.RESOURCE_NOT_FOUND


class TestLifecycle:
    """Закрытие: процесс выходит по концу ввода, каталог настроек удалён."""

    async def test_close_exits_the_process_and_removes_the_workdir(
        self, agents: Agents
    ) -> None:
        process = await agents.start(RecordingUpdates())
        workdir = process.workdir
        assert workdir.settings_path.is_file()
        await process.open_session()

        await process.close()
        assert not process.alive()
        assert not workdir.exists()


class TestHistory:
    """История в Postgres: воспроизведение под messageId вопросов, закрытие
    и удаление сессии, ветка до вопроса."""

    async def test_resume_with_replay_shows_the_conversation(
        self, agents: Agents
    ) -> None:
        process = await agents.start(RecordingUpdates(), "holder")
        opened = await process.open_session()
        session_id = opened.session_id
        text = prompt_text(ScenarioName.ANSWER)
        question, _ = await agents.turn(process, session_id, text)
        await process.close()

        replay = RecordingUpdates()
        observer = await agents.start(replay, "observer")
        await observer.resume_session(session_id, True)

        asked = agents.questions[observer].ids
        assert asked == [question]
        assert "plain streamed answer" in "".join(replay.texts())

        thinking = prompt_text(ScenarioName.THINKING)
        _, again = await agents.turn(observer, session_id, thinking)
        assert again.stop_reason == "end_turn"

    async def test_resume_without_replay_shows_nothing(self, agents: Agents) -> None:
        process = await agents.start(RecordingUpdates(), "holder")
        opened = await process.open_session()
        text = prompt_text(ScenarioName.ANSWER)
        await agents.turn(process, opened.session_id, text)
        await process.close()

        updates = RecordingUpdates()
        successor = await agents.start(updates, "successor")
        response = await successor.resume_session(opened.session_id, False)

        assert response.config_options, response
        assert not agents.questions[successor].ids
        assert not updates.texts()

        thinking = prompt_text(ScenarioName.THINKING)
        _, again = await agents.turn(successor, opened.session_id, thinking)
        assert again.stop_reason == "end_turn"

    async def test_close_releases_the_session_for_another_process(
        self, agents: Agents
    ) -> None:
        holder = await agents.start(RecordingUpdates(), "holder")
        opened = await holder.open_session()
        session_id = opened.session_id
        text = prompt_text(ScenarioName.ANSWER)
        await agents.turn(holder, session_id, text)

        observer = await agents.start(RecordingUpdates(), "observer")
        await observer.resume_session(session_id, True)
        with pytest.raises(AgentRequestError, match="held by another process"):
            await observer.prompt(session_id, [_text(text)])

        await holder.close_session(session_id)
        _, idle = await agents.turn(observer, session_id, text)
        assert idle.stop_reason == "end_turn"

    async def test_delete_removes_the_session(self, agents: Agents) -> None:
        process = await agents.start(RecordingUpdates())
        opened = await process.open_session()
        session_id = opened.session_id
        await agents.turn(process, session_id, prompt_text(ScenarioName.ANSWER))
        await process.close_session(session_id)
        await process.delete_session(session_id)

        with pytest.raises(AgentRequestError) as refused:
            await process.resume_session(session_id, True)

        assert refused.value.code == AcpErrorCode.RESOURCE_NOT_FOUND

    async def test_fork_by_the_message_id_drops_the_question_and_its_answer(
        self, agents: Agents
    ) -> None:
        process = await agents.start(RecordingUpdates())
        opened = await process.open_session()
        session_id = opened.session_id
        answer = prompt_text(ScenarioName.ANSWER)
        thinking = prompt_text(ScenarioName.THINKING_ANSWER)
        first, _ = await agents.turn(process, session_id, answer)
        second, _ = await agents.turn(process, session_id, thinking)

        branch = await process.fork(session_id, second)
        assert branch.session_id != session_id

        replay = RecordingUpdates()
        reader = await agents.start(replay, "reader")
        await reader.resume_session(branch.session_id, True)

        assert agents.questions[reader].ids == [first]
        assert "answer the question" not in "".join(replay.texts())


def _text(text: str) -> schema.TextContentBlock:
    return schema.TextContentBlock(text=text)
