"""Транспорт и жизненный цикл клиента ACP против настоящего `boba acp` с
FakeLlmApp и историей в Postgres стенда: новая сессия и ход с потоком кусков,
отмена идущего хода, разрешение по умолчанию, закрытие процесса; история —
`session/load` с воспроизведением под id вопросов клиента, `session/close`,
`session/delete`, ветка `session/fork` по id вопроса."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from acp import text_block
from acp.schema import (
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UserMessageChunk,
)
from acp_stand import AgentHistoryStand, AgentStand, FakeModel, RecordingUpdates

from boba.chainlit.agent.client import (
    AgentClient,
    AgentProcess,
    AgentRequestError,
    JsonRpcCode,
)
from boba.chainlit.agent.settings import AgentWorkdirs, SettingsFileBuilder
from boba.stand.site import Stand
from boba.stand.ui.fake_llm import ScenarioName

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
    """Процессы агента теста: один файл настроек, несколько запусков."""

    def __init__(self, root: Path, model: FakeModel, stand: Stand) -> None:
        self._root = root
        self._model = model
        self._stand = AgentStand(root, AgentHistoryStand(stand).history())
        self._builder = SettingsFileBuilder(self._stand.config())
        self.started: list[AgentProcess] = []

    async def start(self, updates: RecordingUpdates, thread: str = "t") -> AgentProcess:
        chat = self._model.chat_settings()
        workdir = AgentWorkdirs(self._root).create(thread)
        workdir.write(self._builder.build(thread, chat, {}, "", []), chat.system_prompt)
        process = AgentProcess(
            self._stand.command(), workdir, AgentClient(updates), "0.0"
        )
        await process.start()
        self.started.append(process)

        return process

    async def close_all(self) -> None:
        for process in self.started:
            await process.close()


@pytest.fixture
async def agents(
    tmp_path: Path, model: FakeModel, stand: Stand
) -> AsyncIterator[Agents]:
    made = Agents(tmp_path / "agents", model, stand)
    try:
        yield made
    finally:
        await made.close_all()


def prompt_text(scenario: ScenarioName) -> str:
    return f"please {scenario.value}"


class TestTurn:
    """Сессия и ход: куски ответа приходят приёмнику до ответа на prompt."""

    async def test_prompt_streams_chunks_and_ends_the_turn(
        self, agents: Agents
    ) -> None:
        updates = RecordingUpdates()
        process = await agents.start(updates)
        session_id = await process.new_session()
        assert session_id

        response = await process.prompt(
            session_id, [_text(prompt_text(ScenarioName.THINKING_ANSWER))], _id()
        )
        assert response.stop_reason == "end_turn"

        texts = updates.texts()
        assert texts, "no content chunks arrived"
        joined = "".join(texts)
        assert "answer the question" in joined
        assert "reason about the question" in joined
        assert all(sid == session_id for sid, _ in updates.updates)

    async def test_tool_call_of_an_unknown_tool_is_reported_as_failed(
        self, agents: Agents
    ) -> None:
        updates = RecordingUpdates()
        process = await agents.start(updates)
        session_id = await process.new_session()
        spec = {"name": "no_such_tool", "arguments": {"x": 1}}
        text = f"{ScenarioName.CALL.value} {json.dumps(spec)}"

        response = await process.prompt(session_id, [_text(text)], _id())
        assert response.stop_reason == "end_turn"

        starts = [u for _, u in updates.updates if isinstance(u, ToolCallStart)]
        progress = [u for _, u in updates.updates if isinstance(u, ToolCallProgress)]
        assert starts, "tool_call did not arrive"
        assert starts[0].title.startswith("no_such_tool")
        assert any(p.status == "failed" for p in progress)
        assert not updates.permissions, "bypassPermissions must not ask"

    async def test_cancel_stops_the_turn_with_cancelled(self, agents: Agents) -> None:
        updates = RecordingUpdates()
        process = await agents.start(updates)
        session_id = await process.new_session()

        spec = {"name": "no_such_tool", "arguments": {}}
        text = f"{ScenarioName.LOOP.value} {json.dumps(spec)}"
        turn = asyncio.create_task(process.prompt(session_id, [_text(text)], _id()))
        await asyncio.wait_for(updates.first_update.wait(), 30)
        await process.cancel(session_id)

        response = await asyncio.wait_for(turn, 30)
        assert response.stop_reason == "cancelled"

    async def test_unknown_session_is_refused(self, agents: Agents) -> None:
        process = await agents.start(RecordingUpdates())
        with pytest.raises(AgentRequestError) as refused:
            await process.prompt("no-such-session", [_text("hi")], _id())

        assert refused.value.code == JsonRpcCode.RESOURCE_NOT_FOUND


class TestLifecycle:
    """Закрытие: процесс выходит по концу ввода, каталог настроек удалён."""

    async def test_close_exits_the_process_and_removes_the_workdir(
        self, agents: Agents
    ) -> None:
        process = await agents.start(RecordingUpdates())
        workdir = process.workdir
        assert workdir.settings_path.is_file()
        await process.new_session()

        await process.close()
        assert not process.alive()
        assert not workdir.exists()


class TestHistory:
    """История в Postgres: загрузка с воспроизведением под id вопросов
    клиента, закрытие и удаление сессии, ветка по id вопроса."""

    async def test_load_replays_the_conversation_under_client_ids(
        self, agents: Agents
    ) -> None:
        first = RecordingUpdates()
        process = await agents.start(first, "holder")
        session_id = await process.new_session()
        question = _id()
        await process.prompt(
            session_id, [_text(prompt_text(ScenarioName.ANSWER))], question
        )
        await process.close()

        second = RecordingUpdates()
        observer = await agents.start(second, "observer")
        await observer.load_session(session_id)

        user_chunks = [u for _, u in second.updates if isinstance(u, UserMessageChunk)]
        assert [u.message_id for u in user_chunks] == [question]
        assert "plain streamed answer" in "".join(second.texts())

        again = await observer.prompt(
            session_id, [_text(prompt_text(ScenarioName.THINKING))], _id()
        )
        assert again.stop_reason == "end_turn"

    async def test_resume_opens_the_session_without_replay(
        self, agents: Agents
    ) -> None:
        process = await agents.start(RecordingUpdates(), "holder")
        session_id = await process.new_session()
        await process.prompt(
            session_id, [_text(prompt_text(ScenarioName.ANSWER))], _id()
        )
        await process.close()

        updates = RecordingUpdates()
        successor = await agents.start(updates, "successor")
        response = await successor.resume_session(session_id)

        assert response.config_options, response
        assert not [u for _, u in updates.updates if isinstance(u, UserMessageChunk)]
        assert not updates.texts()

        again = await successor.prompt(
            session_id, [_text(prompt_text(ScenarioName.THINKING))], _id()
        )
        assert again.stop_reason == "end_turn"

    async def test_close_releases_the_session_for_another_process(
        self, agents: Agents
    ) -> None:
        holder = await agents.start(RecordingUpdates(), "holder")
        session_id = await holder.new_session()
        await holder.prompt(
            session_id, [_text(prompt_text(ScenarioName.ANSWER))], _id()
        )

        observer = await agents.start(RecordingUpdates(), "observer")
        await observer.load_session(session_id)
        with pytest.raises(AgentRequestError, match="held by another process"):
            await observer.prompt(
                session_id, [_text(prompt_text(ScenarioName.ANSWER))], _id()
            )

        await holder.close_session(session_id)
        response = await observer.prompt(
            session_id, [_text(prompt_text(ScenarioName.ANSWER))], _id()
        )
        assert response.stop_reason == "end_turn"

    async def test_delete_removes_the_session(self, agents: Agents) -> None:
        process = await agents.start(RecordingUpdates())
        session_id = await process.new_session()
        await process.prompt(
            session_id, [_text(prompt_text(ScenarioName.ANSWER))], _id()
        )
        await process.close_session(session_id)
        await process.delete_session(session_id)

        with pytest.raises(AgentRequestError) as refused:
            await process.load_session(session_id)

        assert refused.value.code == JsonRpcCode.RESOURCE_NOT_FOUND

    async def test_fork_by_the_question_id_drops_the_question_and_its_answer(
        self, agents: Agents
    ) -> None:
        updates = RecordingUpdates()
        process = await agents.start(updates)
        session_id = await process.new_session()
        first = _id()
        second = _id()
        await process.prompt(
            session_id, [_text(prompt_text(ScenarioName.ANSWER))], first
        )
        await process.prompt(
            session_id, [_text(prompt_text(ScenarioName.THINKING_ANSWER))], second
        )

        forked = await process.fork_session(session_id, second)
        assert forked != session_id

        replay = RecordingUpdates()
        reader = await agents.start(replay, "reader")
        await reader.load_session(forked)
        user_chunks = [u for _, u in replay.updates if isinstance(u, UserMessageChunk)]
        assert [u.message_id for u in user_chunks] == [first]
        assert "answer the question" not in "".join(replay.texts())


def _id() -> str:
    return str(uuid4())


def _text(text: str) -> TextContentBlock:
    return text_block(text)
