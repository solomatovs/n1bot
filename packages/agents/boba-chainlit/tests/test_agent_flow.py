"""Тесты графов flow: подготовка контекста хода и обычный цикл."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any

import pytest
from httpx import AsyncClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from pydantic import Field, ValidationError

from boba.chainlit.agent.flow import (
    GraphSpec,
    PassthroughRephraser,
    PlainGraphBuilder,
    PrefetchCall,
    PrefetchError,
    PrefetchGraphBuilder,
)
from boba.chainlit.infra.config import (
    AppConfig,
    ChatProfileConfig,
    PlainFlowConfig,
    PrefetchFlowConfig,
    SelectedProfile,
)
from boba.chainlit.infra.providers import (
    _flow_tools,
    _graph_builder,
    build_history_view,
    httpx_clients,
)
from boba.toolkit.result import ErrorResult, TableResult, ToolArtifact, pack_result

pytestmark = pytest.mark.anyio

OPENAI = {"base_url": "https://llm.example/v1", "api_key": "token"}

THREAD = RunnableConfig(configurable={"thread_id": "flow-thread"})


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class ScriptedChat(GenericFakeChatModel):
    """Основная модель по сценарию: bind_tools у фейка не реализован."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


class FakeRephraser:
    """Переформулировщик без сети: отдаёт заготовленные варианты."""

    def __init__(self, queries: Sequence[str]) -> None:
        self.queries = queries
        self.asked: list[str] = []

    async def rephrase(self, query: str) -> Sequence[str]:
        self.asked.append(query)
        return self.queries


class RecordingStage:
    """Этап без ленты: запоминает, что подготовка открылась и закрылась."""

    def __init__(self) -> None:
        self.opened = 0
        self.closed: list[Sequence[str]] = []

    async def begin(self) -> None:
        self.opened += 1

    async def end(self, queries: Sequence[str]) -> None:
        self.closed.append(list(queries))


class BrokenRephraser:
    """Переформулировщик, у которого недоступен провайдер."""

    async def rephrase(self, query: str) -> Sequence[str]:
        msg = "provider is down"
        raise RuntimeError(msg)


@tool(response_format="content_and_artifact")
async def fts_probe(
    query: Annotated[str, Field(description="Search query.")],
) -> tuple[str, Any]:
    """Полнотекстовый поиск-заглушка."""
    return pack_result(TableResult(rows=[{"hit": f"fts:{query}"}]))


@tool(response_format="content_and_artifact")
async def vector_probe(
    query: Annotated[str, Field(description="Search query.")],
) -> tuple[str, Any]:
    """Векторный поиск-заглушка."""
    return pack_result(TableResult(rows=[{"hit": f"vector:{query}"}]))


@tool(response_format="content_and_artifact")
async def failing_probe(
    query: Annotated[str, Field(description="Search query.")],
) -> tuple[str, Any]:
    """Поиск, отвечающий отказом инструмента."""
    return pack_result(
        ErrorResult(message="database is down", error_kind="database_unavailable")
    )


@tool(response_format="content_and_artifact")
async def crashing_probe(
    query: Annotated[str, Field(description="Search query.")],
) -> tuple[str, Any]:
    """Поиск, падающий исключением."""
    msg = "sandbox crashed"
    raise RuntimeError(msg)


def _graph(builder: Any, answers: Sequence[str]) -> CompiledStateGraph:
    """Граф на фейковой модели: реальные create_agent, checkpointer и history."""
    scripted: list[AIMessage | str] = []
    for answer in answers:
        scripted.append(AIMessage(content=answer))

    chat = ScriptedChat(messages=iter(scripted))
    tools = [fts_probe, vector_probe]

    spec = GraphSpec(
        chat=chat,
        tools=tools,
        system_prompt="you are a search assistant",
        checkpointer=InMemorySaver(),
        history=build_history_view(frozenset({"fts_probe", "vector_probe"}), 30),
    )
    return builder.build(spec)


def _prefetch_calls(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, AIMessage):
            continue

        for call in message.tool_calls:
            call_id = call["id"]
            if call_id and call_id.startswith(PrefetchCall.PREFIX):
                calls.append(dict(call))

    return calls


def _tool_messages(messages: Sequence[BaseMessage]) -> list[ToolMessage]:
    found: list[ToolMessage] = []
    for message in messages:
        if isinstance(message, ToolMessage):
            found.append(message)

    return found


class TestPrefetchGraph:
    """Prefetch-граф: ход получает контекст до обращения к основной модели."""

    async def test_turn_prefetches_search_results(self) -> None:
        rephraser = FakeRephraser(["variant one", "variant two"])
        graph = _graph(
            PrefetchGraphBuilder(rephraser, [fts_probe, vector_probe], RecordingStage()),
            answers=["answered with context"],
        )

        result = await graph.ainvoke(
            {"messages": [HumanMessage("how to configure kerberos?")]},
            config=THREAD,
        )
        messages = result["messages"]

        if rephraser.asked != ["how to configure kerberos?"]:
            raise AssertionError(f"rephraser got {rephraser.asked}")

        calls = _prefetch_calls(messages)
        if len(calls) != 4:
            raise AssertionError(f"2 queries x 2 tools == 4 calls, got {len(calls)}")

        names = set()
        for call in calls:
            names.add(call["name"])
        if names != {"fts_probe", "vector_probe"}:
            raise AssertionError(f"both tools are called, got {names}")

        replies = _tool_messages(messages)
        if len(replies) != 4:
            raise AssertionError(f"4 tool messages expected, got {len(replies)}")

        revived = ToolArtifact.revive(replies[0].artifact)
        if not isinstance(revived, TableResult):
            raise AssertionError(f"artifact is TableResult, got {type(revived)}")

        if not isinstance(messages[-1], AIMessage):
            raise AssertionError("last message is the model answer")
        if messages[-1].content != "answered with context":
            raise AssertionError(f"answer survived: {messages[-1].content!r}")

    async def test_stage_wraps_the_preparation(self) -> None:
        """Этап открывается до подготовки и закрывается запросами, что ушли в поиск."""
        rephraser = FakeRephraser(["variant one", "variant two"])
        stage = RecordingStage()
        graph = _graph(
            PrefetchGraphBuilder(rephraser, [fts_probe], stage),
            answers=["answered"],
        )

        await graph.ainvoke(
            {"messages": [HumanMessage("question")]},
            config=THREAD,
        )

        if stage.opened != 1:
            raise AssertionError(f"этап открывается один раз, а не {stage.opened}")

        if stage.closed != [["variant one", "variant two"]]:
            raise AssertionError(f"этап закрыт запросами {stage.closed}")

    async def test_stage_closes_when_preparation_fails(self) -> None:
        """Сбой подготовки не оставляет этап открытым висеть в ленте."""
        stage = RecordingStage()
        graph = _graph(
            PrefetchGraphBuilder(BrokenRephraser(), [fts_probe], stage),
            answers=["never reached"],
        )

        with pytest.raises(PrefetchError):
            await graph.ainvoke(
                {"messages": [HumanMessage("question")]},
                config=THREAD,
            )

        if stage.opened != 1:
            raise AssertionError("этап был открыт")
        if stage.closed != [[]]:
            raise AssertionError(f"этап закрыт без запросов, получено {stage.closed}")

    async def test_query_goes_as_is_without_rephraser(self) -> None:
        """Профиль без модели-переформулировщика ищет по запросу пользователя."""
        stage = RecordingStage()
        graph = _graph(
            PrefetchGraphBuilder(
                PassthroughRephraser(), [fts_probe, vector_probe], stage
            ),
            answers=["answered"],
        )

        result = await graph.ainvoke(
            {"messages": [HumanMessage("как настроить kerberos?")]},
            config=THREAD,
        )

        calls = _prefetch_calls(result["messages"])
        if len(calls) != 2:
            raise AssertionError(f"один запрос в каждый инструмент, а не {len(calls)}")

        asked = set()
        for call in calls:
            asked.add(call["args"]["query"])
        if asked != {"как настроить kerberos?"}:
            raise AssertionError(f"в поиск ушёл не исходный запрос: {asked}")

        if stage.closed != [["как настроить kerberos?"]]:
            raise AssertionError(f"этап подписан запросами {stage.closed}")

    async def test_every_turn_is_prefetched(self) -> None:
        rephraser = FakeRephraser(["variant"])
        graph = _graph(
            PrefetchGraphBuilder(rephraser, [fts_probe], RecordingStage()),
            answers=["first answer", "second answer"],
        )

        first = await graph.ainvoke(
            {"messages": [HumanMessage("first question")]},
            config=THREAD,
        )
        second = await graph.ainvoke(
            {"messages": [HumanMessage("follow-up question")]},
            config=THREAD,
        )

        if len(_prefetch_calls(first["messages"])) != 1:
            raise AssertionError("первый ход обязан готовить контекст")

        if len(_prefetch_calls(second["messages"])) != 2:
            raise AssertionError(
                "второй вопрос обязан добрать контекст: "
                f"{len(_prefetch_calls(second['messages']))} вызовов"
            )

        if rephraser.asked != ["first question", "follow-up question"]:
            raise AssertionError(f"запросы каждого хода: {rephraser.asked}")

    async def test_search_error_result_fails_the_turn(self) -> None:
        rephraser = FakeRephraser(["variant"])
        graph = _graph(
            PrefetchGraphBuilder(rephraser, [failing_probe], RecordingStage()),
            answers=["never reached"],
        )

        with pytest.raises(PrefetchError, match="database is down"):
            await graph.ainvoke(
                {"messages": [HumanMessage("question")]},
                config=THREAD,
            )

    async def test_search_crash_fails_the_turn(self) -> None:
        rephraser = FakeRephraser(["variant"])
        graph = _graph(
            PrefetchGraphBuilder(rephraser, [crashing_probe], RecordingStage()),
            answers=["never reached"],
        )

        with pytest.raises(PrefetchError):
            await graph.ainvoke(
                {"messages": [HumanMessage("question")]},
                config=THREAD,
            )

    async def test_rephraser_failure_fails_the_turn(self) -> None:
        graph = _graph(
            PrefetchGraphBuilder(BrokenRephraser(), [fts_probe], RecordingStage()),
            answers=["never reached"],
        )

        with pytest.raises(PrefetchError, match="provider is down"):
            await graph.ainvoke(
                {"messages": [HumanMessage("question")]},
                config=THREAD,
            )


class TestPlainGraph:
    """Plain-граф: обычный цикл без подготовки контекста."""

    async def test_no_prefetch_happens(self) -> None:
        graph = _graph(PlainGraphBuilder(), answers=["plain answer"])

        result = await graph.ainvoke(
            {"messages": [HumanMessage("question")]},
            config=THREAD,
        )
        messages = result["messages"]

        if _prefetch_calls(messages):
            raise AssertionError("plain graph must not prefetch")

        if messages[-1].content != "plain answer":
            raise AssertionError(f"answer survived: {messages[-1].content!r}")


class TestFlowConfig:
    """Секция flow профиля: дефолт, разбор prefetch и согласие с tools."""

    def _profile(self, **kw: Any) -> ChatProfileConfig:
        base: dict[str, Any] = {
            "display_name": "Profile",
            "description": "test profile",
            "provider": OPENAI,
            "model": "test-model",
        }
        base.update(kw)
        return ChatProfileConfig.model_validate(base)

    def test_flow_defaults_to_plain(self) -> None:
        profile = self._profile()
        if not isinstance(profile.flow, PlainFlowConfig):
            raise AssertionError(f"default flow is plain, got {profile.flow}")

    def test_prefetch_flow_is_parsed(self) -> None:
        profile = self._profile(
            tools=["kb_fts_search", "kb_vector_search", "web_fetch_page"],
            flow={
                "kind": "prefetch",
                "tools": ["kb_fts_search", "kb_vector_search"],
                "rephraser": {
                    "provider": OPENAI,
                    "model": "small-model",
                    "system_prompt": "rephrase",
                    "queries": 3,
                },
            },
        )

        flow = profile.flow
        if not isinstance(flow, PrefetchFlowConfig):
            raise AssertionError(f"flow is prefetch, got {flow}")
        if flow.rephraser is None:
            raise AssertionError("секция rephraser разобрана")
        if flow.rephraser.queries != 3:
            raise AssertionError(f"queries == 3, got {flow.rephraser.queries}")

    def test_flow_tool_outside_profile_is_config_error(self) -> None:
        with pytest.raises(ValidationError, match="flow tools"):
            self._profile(
                tools=["web_fetch_page"],
                flow={
                    "kind": "prefetch",
                    "tools": ["kb_fts_search"],
                    "rephraser": {
                        "provider": OPENAI,
                        "model": "small-model",
                        "system_prompt": "rephrase",
                        "queries": 3,
                    },
                },
            )

    def test_wildcard_profile_accepts_any_flow_tools(self) -> None:
        profile = self._profile(
            tools=["*"],
            flow={
                "kind": "prefetch",
                "tools": ["kb_fts_search"],
                "rephraser": {
                    "provider": OPENAI,
                    "model": "small-model",
                    "system_prompt": "rephrase",
                    "queries": 2,
                },
            },
        )
        if not isinstance(profile.flow, PrefetchFlowConfig):
            raise AssertionError("prefetch flow is parsed with wildcard tools")

    def test_client_key_is_stable(self) -> None:
        if PrefetchFlowConfig.client_key("search") != "search:flow":
            raise AssertionError("client key is '<profile>:flow'")


class TestProviderAssembly:
    """Сборка на стороне провайдеров: билдер по профилю и клиент flow."""

    def _selected(self, **profile_kw: Any) -> SelectedProfile:
        base: dict[str, Any] = {
            "display_name": "Search",
            "description": "search profile",
            "provider": OPENAI,
            "model": "test-model",
            "tools": ["*"],
        }
        base.update(profile_kw)
        config = ChatProfileConfig.model_validate(base)
        return SelectedProfile(name="search", config=config)

    def _flow(self, **kw: Any) -> dict[str, Any]:
        flow: dict[str, Any] = {
            "kind": "prefetch",
            "tools": ["fts_probe"],
            "rephraser": {
                "provider": OPENAI,
                "model": "small-model",
                "system_prompt": "rephrase",
                "queries": 2,
            },
        }
        flow.update(kw)
        return flow

    def test_plain_profile_gets_plain_builder(self) -> None:
        builder = _graph_builder(self._selected(), {"search": AsyncClient()}, [])
        if not isinstance(builder, PlainGraphBuilder):
            raise AssertionError(f"plain builder expected, got {type(builder)}")

    def test_prefetch_profile_gets_prefetch_builder(self) -> None:
        selected = self._selected(flow=self._flow())
        clients = {"search": AsyncClient(), "search:flow": AsyncClient()}

        builder = _graph_builder(selected, clients, [fts_probe])
        if not isinstance(builder, PrefetchGraphBuilder):
            raise AssertionError(f"prefetch builder expected, got {type(builder)}")

    def test_flow_tool_missing_in_session_is_build_error(self) -> None:
        with pytest.raises(RuntimeError, match="not available"):
            _flow_tools(["kb_fts_search"], [fts_probe])

    async def test_httpx_clients_carry_flow_client(
        self, app_config: AppConfig
    ) -> None:
        profile = self._selected(flow=self._flow()).config
        config = app_config.model_copy(update={"profiles": {"search": profile}})

        clients_gen = httpx_clients(config)
        clients = await anext(clients_gen)
        try:
            if set(clients) != {"search", "search:flow"}:
                raise AssertionError(f"flow client is created, got {set(clients)}")
        finally:
            await anext(clients_gen, None)
