"""Локальный чат-бэкенд целиком на боевом конфиге (pytest -m integration).

Модель берётся из профиля с локальным бэкендом рабочего конфига, стек — тот же,
что в приложении: фабрика провайдера, мост ProviderChatModel, агент langgraph,
инструмент с обязательной подписью вызова. Проверяется, что локальная модель
ведёт ход с инструментом до ответа и что лента получает тот же поток событий,
что и от удалённого провайдера.

Запуск: BOBA_CONFIG_PATH=... pytest -m integration tests/test_local_chat_integration.py
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

import pytest
from conftest import RecordedTurn
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from boba.chainlit.chat.tracing import AgentTracer
from boba.chainlit.chat.turn import TurnState
from boba.chainlit.domain.fields import StepField
from boba.chainlit.infra.config import AppConfig
from boba.chat.provider import ChatSampling, LocalChatConfig
from boba.llm.bridge import ChatProviderFactory, ProviderChatModel
from boba.llm.local import OnnxChatRuntime
from boba.settings import bind, build_app_config
from boba.toolkit.calls import ToolIntent
from boba.toolrun.call_id import ToolCallIdField
from boba.toolrun.intent import ToolIntentField
from boba.toolrun.run_log import NoCallScope, ToolRunLogger
from boba.toolrun.wrapping import ToolAsyncBody

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

THREAD = "55555555-5555-5555-5555-555555555555"
TURN = "66666666-6666-6666-6666-666666666666"

TURN_TIMEOUT_SEC = 900.0
"""Три обращения к модели на CPU: минуты, не секунды."""


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Заглушка сессионной фикстуры: БД этому тесту не нужна."""


@pytest.fixture
async def http_context() -> None:
    from chainlit.context import init_http_context

    init_http_context()


def _local_model_dir() -> str:
    """Каталог модели из первого профиля с локальным бэкендом рабочего конфига."""
    config_path = os.environ.get("BOBA_CONFIG_PATH")
    if not config_path:
        pytest.skip("BOBA_CONFIG_PATH не задан")

    built = build_app_config(config_path=Path(config_path))
    app_config = bind(built, path="app", model=AppConfig)

    for profile in app_config.profiles.values():
        if isinstance(profile.backend, LocalChatConfig):
            model_dir = profile.backend.model_dir
            if not (Path(model_dir) / "genai_config.json").is_file():
                pytest.skip(f"нет весов локальной модели: {model_dir}")

            return model_dir

    pytest.skip("в конфиге нет профиля с локальным бэкендом")


@tool
async def kb_probe(
    query: Annotated[str, Field(description="Search query.")],
) -> str:
    """Поиск по базе знаний: отдаёт найденную страницу."""
    return f"KB-42: Kerberos SSO в Confluence настраивается на странице 'Kerberos' ({query})"


def _chat(model_dir: str) -> ProviderChatModel:
    cfg = LocalChatConfig(provider="local", model_dir=model_dir)
    provider = ChatProviderFactory.build(
        cfg,
        model=Path(model_dir).name,
        client=None,
        runtime=OnnxChatRuntime(model_dir),
    )

    return ProviderChatModel(
        provider=provider,
        sampling=ChatSampling(max_tokens=1024),
        model_name=Path(model_dir).name,
    )


class TestLocalChatTurn:
    """Ход агента на локальной модели: инструмент, подпись вызова, ответ."""

    async def test_local_model_drives_a_tool_turn(self, http_context: None) -> None:
        model_dir = _local_model_dir()

        # обвязка как в load_tools: подпись вызова снимает ToolRunLogger
        tools = [kb_probe]
        ToolCallIdField.attach_all(tools)
        ToolIntentField.attach_all(tools)
        ToolRunLogger.guard_all(tools, lambda tool, call_id: None, NoCallScope.enter)
        ToolAsyncBody.ensure_all(tools)

        agent = create_agent(
            model=_chat(model_dir),
            tools=tools,
            system_prompt=(
                "Ты поисковый ассистент. На вопросы о продуктах сначала ищи "
                "инструментом kb_probe, потом отвечай по найденному."
            ),
            checkpointer=InMemorySaver(),
        )

        turn = RecordedTurn.recording(THREAD, TURN)
        sink = turn.recording_sink
        tracer = AgentTracer(turn.feed, TurnState())

        config = RunnableConfig(
            configurable={"thread_id": "local-turn"},
            callbacks=[tracer],
        )
        chunks = 0
        async for _chunk, _meta in agent.astream(
            {"messages": [HumanMessage("как настроить kerberos в confluence?")]},
            config=config,
            stream_mode="messages",
        ):
            chunks += 1

        state = await agent.aget_state(config)
        messages: list[Any] = state.values["messages"]

        calls = [m for m in messages if isinstance(m, AIMessage) and m.tool_calls]
        if not calls:
            raise AssertionError(f"модель не позвала инструмент: {messages}")

        first = calls[0].tool_calls[0]
        if first["name"] != "kb_probe":
            raise AssertionError(f"позван не тот инструмент: {first}")

        if not ToolIntent.of(first["args"]):
            raise AssertionError(f"подпись вызова не заполнена: {first['args']}")

        replies = [m for m in messages if isinstance(m, ToolMessage)]
        if not replies:
            raise AssertionError("результат инструмента не вернулся в историю")

        final = messages[-1]
        if not isinstance(final, AIMessage) or not str(final.content).strip():
            raise AssertionError(f"ход не дошёл до ответа: {final}")

        if chunks < 10:
            raise AssertionError(f"ответ не стримился: {chunks} чанков")

        names = [str(step.get(StepField.NAME, "")) for step in sink.steps]
        if not any("kb_probe" in name for name in names):
            raise AssertionError(f"шаг инструмента не попал в ленту: {names}")

        if not any("thinking" in name for name in names):
            raise AssertionError(f"рассуждения модели не попали в ленту: {names}")
