"""Свежесть описания и списков сервера MCP настоящим путём: `boba acp` v2 с
фейковой моделью → ретранслятор чата (mcp/message) → сервер без потоков
смены списков (drifting_server.py) по stdio.

Сервер не обещает смены списков, а ретранслятор отдаёт ответы со сроком
свежести 0, поэтому агент перечитывает описание и списки в начале каждого
хода и объявляет модели разницу вложениями дословно по docs/prompt_assembly
(texts/attachments/deferred_tools_delta.md, mcp_instructions_delta.md):
инструмент, появившийся молча, ресурсы для модели, сервер, который
пропал, — и он же, вернувшийся. Инструкции, сменившиеся у сервера, агент
объявляет после перезапуска чата, когда новый ретранслятор согласует сервер
заново и сессия возобновляется.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import pytest
from acp_stand import Agents, FakeModel, ModelRequests, RecordingUpdates
from aid_stand import TabSession
from drifting_server import DriftingFile

from boba.aid.agent.client import AgentProcess
from boba.mcp_client.client import (
    McpServerConfig,
    McpServerLimits,
    McpServersConfig,
    StdioCommand,
)
from boba.stand.signin import SignInStand
from boba.stand.site import Stand
from boba.stand.ui.fake_llm import ScenarioName

pytestmark = pytest.mark.anyio

SERVER = "drifting"
SCRIPT = Path(__file__).with_name("drifting_server.py")


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Контекст chainlit этим тестам не нужен: они живут ниже обработчиков."""


@dataclass(frozen=True)
class DriftingState:
    """Каталог состояния сервера теста: инструкции и признак «сервер лежит»."""

    root: Path

    def instruct(self, text: str) -> None:
        (self.root / DriftingFile.INSTRUCTIONS).write_text(text, encoding="utf-8")

    def down(self) -> None:
        (self.root / DriftingFile.DOWN).touch()

    def up(self) -> None:
        (self.root / DriftingFile.DOWN).unlink()

    def servers(self) -> McpServersConfig:
        endpoint = StdioCommand(
            command=sys.executable, args=(str(SCRIPT), str(self.root))
        )
        config = McpServerConfig(
            endpoint=endpoint,
            limits=McpServerLimits(connect_timeout_sec=20.0, call_timeout_sec=60.0),
        )

        return McpServersConfig(servers={SERVER: config})


class DriftingTexts:
    """Вложения, которыми агент объявляет модели перемены сервера drifting."""

    AMBIENT: ClassVar[str] = (
        "This is ambient context — do not narrate it to the user unless they "
        "ask or it is directly relevant to their request."
    )
    TOOLS: ClassVar[tuple[str, ...]] = (
        "mcp__drifting__die",
        "mcp__drifting__drift",
        "mcp__drifting__echo",
        "mcp__drifting__instruct",
        "mcp__drifting__publish",
    )

    def instructions(self, text: str) -> str:
        return (
            "<system-reminder>\n"
            "# MCP Server Instructions\n\n"
            "The following MCP servers have provided instructions for how to use "
            "their tools and resources:\n\n"
            f"## {SERVER}\n"
            f"{text}\n"
            "</system-reminder>"
        )

    def instructions_gone(self) -> str:
        return (
            "<system-reminder>\n"
            "The following MCP servers have disconnected. Their instructions above "
            "no longer apply:\n"
            f"{SERVER}\n\n"
            f"{self.AMBIENT}\n"
            "</system-reminder>"
        )

    def added(self, lines: str) -> str:
        return (
            "<system-reminder>\n"
            f"The following tools are now available:\n{lines}\n"
            "</system-reminder>"
        )

    def server_tools_gone(self, names: str) -> str:
        return (
            "<system-reminder>\n"
            "The following tools are no longer available (their MCP server "
            f"disconnected). Do not call them:\n{names}\n\n"
            f"{self.AMBIENT}\n"
            "</system-reminder>"
        )

    def server_tools_back(self) -> str:
        return (
            "<system-reminder>\n"
            "5 tools are available again (MCP server reconnected — names announced "
            "earlier in this conversation): mcp__drifting__* (5).\n"
            "</system-reminder>"
        )


class TestFreshnessThroughRelay:
    """Агент видит перемены сервера в начале следующего хода, хотя сервер о
    них не сообщает: сверка того, что модель прочитала, — дословная."""

    FIRST: ClassVar[str] = "Use echo to repeat a text; drift moves the tool extra."
    SECOND: ClassVar[str] = "Echo is the only tool to trust; ignore extra."
    RESOURCES: ClassVar[str] = (
        "The following tools are now available:\n"
        "list_mcp_resources: List the resources and resource templates"
    )

    @pytest.fixture
    def state(self, tmp_path: Path) -> DriftingState:
        root = tmp_path / "drifting"
        root.mkdir()
        made = DriftingState(root)
        made.instruct(self.FIRST)

        return made

    @pytest.fixture
    async def agents(
        self,
        tmp_path: Path,
        model: FakeModel,
        stand: Stand,
        state: DriftingState,
        auth_token: str,
    ) -> AsyncIterator[Agents]:
        """Процессы агента над сервером drifting; auth_token — секрет входа
        стенда: им подписан вход вкладки, чью регистрацию получает агент."""
        made = Agents(tmp_path / "agents", model, stand, state.servers())
        try:
            yield made
        finally:
            await made.close_all()

    @staticmethod
    def _call(name: str, arguments: Mapping[str, Any]) -> str:
        spec = {"name": name, "arguments": dict(arguments)}

        return f"{ScenarioName.CALL.value} {json.dumps(spec)}"

    async def _process(self, agents: Agents, thread: str) -> AgentProcess:
        tab = TabSession()
        tab.password_sign_in(frozenset({"dev"}))
        servers = agents.registrations.register(
            tab.registration(uuid4(), SignInStand.PROFILE), [SERVER]
        )

        return await agents.start(RecordingUpdates(), thread, servers)

    async def _session(self, agents: Agents, thread: str) -> tuple[AgentProcess, str]:
        process = await self._process(agents, thread)
        opened = await process.open_session()

        return process, opened.session_id

    async def test_tool_that_appears_silently_is_announced_next_turn(
        self, agents: Agents, model: FakeModel
    ) -> None:
        """Сервер без listChanged добавил инструмент — модель видит его в
        следующем ходе с описанием; убрал — видит снятым."""
        process, session_id = await self._session(agents, "drift")
        requests = ModelRequests(model)
        texts = DriftingTexts()

        await agents.turn(process, session_id, self._call("mcp__drifting__drift", {}))
        before = requests.offered_names()
        await agents.turn(process, session_id, ScenarioName.ANSWER.value)
        added = requests.saw(
            texts.added("mcp__drifting__extra: Appears only after drift.")
        )
        offered = requests.offered_names()
        await agents.turn(process, session_id, self._call("mcp__drifting__drift", {}))
        await agents.turn(process, session_id, ScenarioName.ANSWER.value)
        gone = requests.saw(texts.server_tools_gone("mcp__drifting__extra"))
        after = requests.offered_names()

        assert "mcp__drifting__extra" not in before, before
        assert added, requests.read()
        assert "mcp__drifting__extra" in offered, offered
        assert gone, requests.read()
        assert "mcp__drifting__extra" not in after, after

    async def test_resources_for_the_model_appear_next_turn(
        self, agents: Agents, model: FakeModel
    ) -> None:
        """Ресурс появился у сервера молча — в следующем ходе у модели есть
        инструменты ресурсов, и они его читают."""
        process, session_id = await self._session(agents, "publish")
        requests = ModelRequests(model)

        await agents.turn(process, session_id, self._call("mcp__drifting__publish", {}))
        before = requests.offered_names()
        read = {"server": SERVER, "uri": "drifting://notes"}
        await agents.turn(process, session_id, self._call("read_mcp_resource", read))
        offered = requests.offered_names()
        notes = requests.tool_result()

        assert "read_mcp_resource" not in before, before
        assert "list_mcp_resources" in offered, offered
        assert requests.saw(self.RESOURCES), requests.read()
        assert "the notes of the drifting server" in notes, notes

    async def test_server_that_drops_and_returns_is_announced(
        self, agents: Agents, model: FakeModel, state: DriftingState
    ) -> None:
        """Сервер пропал между ходами — его инструменты сняты как ушедшие с
        сервером и инструкции отозваны; вернулся — инструменты объявлены
        вернувшимися, инструкции объявлены снова."""
        process, session_id = await self._session(agents, "drop")
        requests = ModelRequests(model)
        texts = DriftingTexts()

        await agents.turn(process, session_id, ScenarioName.ANSWER.value)
        first = requests.saw(texts.instructions(self.FIRST))
        state.down()
        await agents.turn(process, session_id, self._call("mcp__drifting__die", {}))
        await agents.turn(process, session_id, ScenarioName.ANSWER.value)
        tools_gone = requests.saw(texts.server_tools_gone("\n".join(texts.TOOLS)))
        instructions_gone = requests.saw(texts.instructions_gone())
        while_down = requests.offered_names()
        state.up()
        await agents.turn(process, session_id, ScenarioName.ANSWER.value)
        tools_back = requests.saw(texts.server_tools_back())
        back = requests.offered_names()

        assert first, requests.read()
        assert tools_gone, requests.read()
        assert instructions_gone, requests.read()
        assert "mcp__drifting__echo" not in while_down, while_down
        assert tools_back, requests.read()
        assert requests.read().count("# MCP Server Instructions") == 2
        assert "mcp__drifting__echo" in back, back

    async def test_instructions_changed_live_reach_the_next_turn(
        self, agents: Agents, model: FakeModel
    ) -> None:
        """Сервер сменил инструкции посреди сессии и о том не сообщил:
        ретранслятор перечитывает описание по сроку свежести, и следующий ход
        объявляет модели новый текст вложением."""
        process, session_id = await self._session(agents, "instruct")
        requests = ModelRequests(model)
        texts = DriftingTexts()
        changed = {"text": self.SECOND}

        await agents.turn(
            process, session_id, self._call("mcp__drifting__instruct", changed)
        )
        await agents.turn(process, session_id, ScenarioName.ANSWER.value)

        assert requests.saw(texts.instructions(self.FIRST)), requests.read()
        assert requests.saw(texts.instructions(self.SECOND)), requests.read()
        assert "# MCP Server Instructions" not in requests.system()

    async def test_changed_instructions_are_announced_when_the_chat_restarts(
        self,
        tmp_path: Path,
        model: FakeModel,
        stand: Stand,
        state: DriftingState,
        auth_token: str,
    ) -> None:
        """Сервер сменил инструкции, чат перезапущен: новый ретранслятор
        согласует сервер заново, возобновлённая сессия объявляет модели новый
        текст вложением, а прежний остаётся в беседе."""
        requests = ModelRequests(model)
        texts = DriftingTexts()
        before = Agents(tmp_path / "before", model, stand, state.servers())
        try:
            process, session_id = await self._session(before, "first")
            await before.turn(process, session_id, ScenarioName.ANSWER.value)
        finally:
            await before.close_all()

        state.instruct(self.SECOND)
        after = Agents(tmp_path / "after", model, stand, state.servers())
        try:
            process = await self._process(after, "second")
            await process.resume_session(session_id, replay=False)
            await after.turn(process, session_id, ScenarioName.ANSWER.value)
        finally:
            await after.close_all()

        assert requests.saw(texts.instructions(self.FIRST)), requests.read()
        assert requests.saw(texts.instructions(self.SECOND)), requests.read()
        assert "# MCP Server Instructions" not in requests.system()
