"""Ход агента на инструментах сервиса: набор ToolSet под запускателем, история по
thread, предел обращений, падение тела как результат для модели. Быстрые тесты идут на
модели по сценарию и субпроцессе фейкового инструмента стенда; интеграционный собирает
инструмент bash запускателем из [tool_launcher] приложения и гоняет локальную
onnx-модель из [agent_stand].

Ошибки стенда: AgentStandError — секции [agent_stand] нет, интеграционный тест
пропускается.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from omegaconf import DictConfig
from pydantic import SecretStr

from boba.agent import Agent, AgentError, AgentLimits, GraphSpec, PlainGraphBuilder
from boba.chat.provider import LocalChatConfig
from boba.config import bind
from boba.llm.bridge import ChatProviderFactory, ProviderChatModel
from boba.llm.local import OnnxChatRuntime
from boba.runtime.launchers import ToolLaunchers
from boba.runtime.plugins import ToolSet
from boba.stand.agent import AgentStand
from boba.stand_core.fake_toolmod import FakeConfig, fake_echo
from boba.tool.shell.tools import BashToolConfig, bash
from boba.toolkit.manifest import LaunchSpec
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller

pytestmark = pytest.mark.anyio

CFG = FakeConfig(token=SecretStr("t0ken"), limit=5)


class ScriptedChat(GenericFakeChatModel):
    """Модель по сценарию: отдаёт заготовленные ответы, инструменты принимает молча."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


class Script:
    """Заготовки ответов модели по сценарию."""

    @staticmethod
    def echo(text: str, call_id: str) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "fake_echo",
                    "args": {"text": text, "repeat": 2},
                    "id": call_id,
                }
            ],
        )

    @staticmethod
    def endless() -> Iterator[AIMessage]:
        number = 0
        while True:
            number += 1
            yield Script.echo("again", f"call-{number}")


class Kinds:
    """Виды сообщений хода одной строкой, для сверки протокола."""

    @staticmethod
    def of(messages: Sequence[BaseMessage]) -> list[str]:
        found: list[str] = []
        for message in messages:
            found.append(type(message).__name__)

        return found


def fake_tools(workdir: Path) -> list[BaseTool]:
    """Инструмент стенда субпроцессом хоста через общий конвейер."""
    values = {
        "provider": "process",
        "workdir": str(workdir),
        "timeout_sec": 60.0,
        "channel_limit_bytes": 1_000_000,
        "stderr_tail_bytes": 4096,
        "kill_grace_sec": 1.0,
    }
    launcher = ProcessToolCaller("fake", ProcessLauncherConfig.model_validate(values))

    return ToolSet(launcher, [CFG]).build([fake_echo])


def agent_of(
    chat: Any, tools: Sequence[BaseTool], saver: InMemorySaver, max_model_calls: int = 4
) -> Agent:
    spec = GraphSpec(
        chat=chat,
        tools=tools,
        system_prompt="Отвечай по данным инструментов.",
        checkpointer=saver,
    )

    return Agent(
        spec, AgentLimits(max_model_calls=max_model_calls), PlainGraphBuilder()
    )


class TestToolSetTurn:
    async def test_tool_call_runs_under_the_launcher(self, tmp_path: Path) -> None:
        chat = ScriptedChat(
            messages=iter(
                [Script.echo("ping", "call-1"), AIMessage(content="ping ping")]
            )
        )
        agent = agent_of(chat, fake_tools(tmp_path), InMemorySaver())

        reply = await agent.ask("t1", "позови эхо")

        assert reply.text == "ping ping"
        assert reply.model_calls == 2
        assert reply.tool_calls == 1
        assert Kinds.of(reply.messages) == [
            "HumanMessage",
            "AIMessage",
            "ToolMessage",
            "AIMessage",
        ]
        result = reply.messages[2]
        assert isinstance(result, ToolMessage)
        assert "ping ping|t0ken" in str(result.content)

    async def test_second_question_sees_the_thread_and_returns_only_new(
        self, tmp_path: Path
    ) -> None:
        chat = ScriptedChat(
            messages=iter([AIMessage(content="первый"), AIMessage(content="второй")])
        )
        saver = InMemorySaver()
        agent = agent_of(chat, fake_tools(tmp_path), saver)

        first = await agent.ask("t2", "раз")
        second = await agent.ask("t2", "два")

        assert first.text == "первый"
        assert second.text == "второй"
        assert Kinds.of(second.messages) == ["HumanMessage", "AIMessage"]

        stored = await saver.aget({"configurable": {"thread_id": "t2"}})
        assert stored is not None
        history = stored["channel_values"]["messages"]
        assert Kinds.of(history) == [
            "HumanMessage",
            "AIMessage",
            "HumanMessage",
            "AIMessage",
        ]

    async def test_forget_drops_the_thread(self, tmp_path: Path) -> None:
        chat = ScriptedChat(messages=iter([AIMessage(content="есть")]))
        saver = InMemorySaver()
        agent = agent_of(chat, fake_tools(tmp_path), saver)
        await agent.ask("t3", "вопрос")

        await agent.forget("t3")

        assert await saver.aget({"configurable": {"thread_id": "t3"}}) is None

    async def test_model_call_limit_is_an_error(self, tmp_path: Path) -> None:
        chat = ScriptedChat(messages=Script.endless())
        agent = agent_of(chat, fake_tools(tmp_path), InMemorySaver(), 3)

        with pytest.raises(AgentError, match="model call limit 3"):
            await agent.ask("t4", "зациклись")

    async def test_body_failure_reaches_the_model_as_a_result(
        self, tmp_path: Path
    ) -> None:
        chat = ScriptedChat(
            messages=iter(
                [
                    Script.echo("crash", "call-1"),
                    AIMessage(content="инструмент упал, отвечаю без него"),
                ]
            )
        )
        agent = agent_of(chat, fake_tools(tmp_path), InMemorySaver())

        reply = await agent.ask("t5", "сломай")

        assert reply.text == "инструмент упал, отвечаю без него"
        failed = reply.messages[2]
        assert isinstance(failed, ToolMessage)
        assert "crash" in str(failed.content)
        assert failed.artifact is not None
        assert failed.artifact.ok is False


@pytest.mark.integration
class TestLocalModelTurn:
    async def test_qwen_runs_bash_through_the_launcher(
        self, raw_config: DictConfig
    ) -> None:
        stand = AgentStand.required()
        launchers = ToolLaunchers.of(raw_config)
        launchers.probe()
        launcher = launchers.launcher_of(
            LaunchSpec(section="bash", modules=("boba.tool.shell.tools",))
        )
        shell = bind(raw_config, BashToolConfig.SECTION, BashToolConfig)
        tools = ToolSet(launcher, [shell]).build([bash])

        runtime = OnnxChatRuntime(stand.model_dir)
        provider = ChatProviderFactory.build(
            LocalChatConfig(kind="local", model_dir=stand.model_dir),
            model="qwen3-4b",
            client=None,
            runtime=runtime,
        )
        chat = ProviderChatModel(
            provider=provider,
            sampling={"max_tokens": stand.max_tokens},
            model_name="qwen3-4b",
        )
        spec = GraphSpec(
            chat=chat,
            tools=tools,
            system_prompt=(
                "Ты помощник оператора. Команды выполняй только инструментом bash "
                "и отвечай по его выводу."
            ),
            checkpointer=InMemorySaver(),
        )
        agent = Agent(spec, AgentLimits(max_model_calls=4), PlainGraphBuilder())
        assert agent.tool_names() == ("bash",)

        reply = await agent.ask(
            "shell", "Выполни команду `echo agent-ok` и скажи, что она напечатала."
        )

        assert reply.tool_calls >= 1
        assert "agent-ok" in reply.text
        assert "ToolMessage" in Kinds.of(reply.messages)
