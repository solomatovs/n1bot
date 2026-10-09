"""Локальный чат-бэкенд целиком на стеке приложения (pytest -m integration).

Модель называет секция [agent_stand] стендового слоя conf/stand.toml: рабочий
конфиг профиля с локальным бэкендом не держит. Стек — тот же, что в
приложении: фабрика провайдера, мост ProviderChatModel, агент langgraph,
инструмент с обязательной подписью вызова. Проверяется, что локальная модель
ведёт ход с инструментом до ответа и что лента получает тот же поток событий,
что и от удалённого провайдера.

Запуск: BOBA_CONFIG_PATH=... BOBA_SITE_PATH=... pytest -m integration
tests/test_local_chat_integration.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, ClassVar, Self

import pytest
from chainlit_stand import RecordedTurn
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from omegaconf import DictConfig
from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.chainlit.agent.bridge import ChatModelBridge
from boba.chainlit.agent.events import TurnEvents
from boba.chainlit.agent.flow import GraphSpec, HistoryView, PlainGraphBuilder
from boba.chainlit.agent.history import CheckpointMessages
from boba.chainlit.domain.fields import StepField
from boba.config import bind
from boba.llm.onnx import OnnxProvider
from boba.llm.providers import ChatModelConfig, LlmProviders, LlmProviderTypes
from boba.stand.refs import StandRefs
from boba.toolkit.calls import ToolIntent
from boba.toolkit.chain import CallAmbient
from boba.toolkit.facade import tool
from boba.toolkit.ports import StreamSpecs
from boba.toolkit.result import MarkdownResult
from boba.toolrun.call_id import CallFields
from boba.toolrun.hosted import ToolHosting
from boba.toolrun.run_log import ToolRunLogger
from boba.toolrun.stream_calls import LocalDagService
from boba.toolrun.streams import CallJournals

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


class AgentStand(BaseModel):
    """Секция [agent_stand]: каталог весов локальной модели и потолок ответа."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    WEIGHTS: ClassVar[str] = "genai_config.json"

    model_dir: Path
    max_tokens: int = Field(gt=0)

    @model_validator(mode="after")
    def _weights_are_placed(self) -> Self:
        manifest = self.model_dir / self.WEIGHTS
        if not manifest.is_file():
            msg = (
                f"[agent_stand] model_dir {self.model_dir}: expected the weights of "
                f"the local model with {self.WEIGHTS}, the file is missing"
            )
            raise ValueError(msg)

        return self


@pytest.fixture(scope="module")
def agent_stand(raw_config: DictConfig) -> AgentStand:
    return bind(raw_config, path="agent_stand", model=AgentStand)


@tool
async def kb_probe(
    query: Annotated[str, Field(description="Search query.")],
) -> MarkdownResult:
    """Поиск по базе знаний: отдаёт найденную страницу."""
    text = (
        f"KB-42: Kerberos SSO в Confluence настраивается на странице 'Kerberos' "
        f"({query})"
    )

    return MarkdownResult(text=text)


def _chat(stand: AgentStand) -> ChatModelBridge:
    cfg = ChatModelConfig(
        provider=OnnxProvider(kind="onnx", model_dir=str(stand.model_dir)),
        model=stand.model_dir.name,
        sampling={"max_tokens": stand.max_tokens},
    )
    providers = LlmProviders(LlmProviderTypes.installed())

    return ChatModelBridge(
        chat_model=providers.chat(cfg),
        sampling=cfg.sampling,
        model_name=cfg.model,
    )


class TestLocalChatTurn:
    """Ход агента на локальной модели: инструмент, подпись вызова, ответ."""

    async def test_local_model_drives_a_tool_turn(
        self, http_context: None, agent_stand: AgentStand
    ) -> None:
        # обвязка как в load_tools: подпись вызова снимает ToolRunLogger
        turn = RecordedTurn.recording(THREAD, TURN)
        sink = turn.recording_sink

        tools = list(ToolHosting().toolset([kb_probe]))
        CallFields().attach_all(tools)
        ambient = CallAmbient()
        ToolRunLogger(CallJournals(None, turn.runs), turn.contexts, ambient).guard_all(
            tools
        )

        spec = GraphSpec(
            chat=_chat(agent_stand),
            service=LocalDagService(
                tools,
                StandRefs.STREAM_CONFIG,
                (),
                StreamSpecs({}),
                ambient,
                turn.calls,
            ),
            system_prompt=(
                "Ты поисковый ассистент. На вопросы о продуктах сначала ищи "
                "инструментом kb_probe, потом отвечай по найденному."
            ),
            checkpoints=CheckpointMessages(InMemorySaver()),
            history=HistoryView(frozenset({"kb_probe"}), 30),
        )
        agent = PlainGraphBuilder().build(spec)

        chunks = 0
        with turn.running() as port:
            config = RunnableConfig(
                configurable={"thread_id": "local-turn"},
                callbacks=[TurnEvents([port.tracer], port.tracer)],
            )
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
