"""Prefetch-flow целиком на боевом конфиге (pytest -m integration).

Граф собирается той же цепочкой провайдеров, что и в приложении: инструменты
исполняет сервис boba-mcp, граф ходит к нему портом чата, переформулировщик
и основная модель ходят к провайдеру из конфига.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import chainlit as cl
import pytest
from chainlit_stand import ServiceTools, SessionTools, ToolService
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from boba.chainlit.agent.bridge import ChatModelBridge
from boba.chainlit.agent.flow import (
    GraphSpec,
    HistoryView,
    LlmRephraser,
    PrefetchGraphBuilder,
)
from boba.chainlit.agent.history import CheckpointMessages
from boba.chainlit.infra.config import AppConfig
from boba.chainlit.infra.providers import (
    llm_providers,
    session_graph_builder,
)
from boba.chat.profiles import ChatSettings, PrefetchFlowConfig, SelectedProfile
from boba.llm.http.openai import OpenAiProvider
from boba.llm.providers import LlmProviders
from boba.llm.schema import SchemaReply
from boba.mcp_client.client import McpCaller
from boba.stand.refs import StandRefs
from boba.stand_core.context import CallStand
from boba.toolkit.calls import CallIdPrefix
from boba.toolkit.result import TableResult, ToolArtifact
from boba.transport.http.connection import HttpConnection, UrlScheme

pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(bool(ToolService.missing()), reason=ToolService.missing()),
]

PROFILE = "search"
"""Профиль конфига, у которого объявлен prefetch-flow."""

QUESTION = "как настроить kerberos для postgres?"

THREAD = RunnableConfig(configurable={"thread_id": "flow-integration"})


@pytest.fixture(scope="module")
def flow_config(app_config: AppConfig) -> PrefetchFlowConfig:
    """Секция flow боевого профиля; без неё проверять нечего."""
    flow = app_config.profiles[PROFILE].flow
    if not isinstance(flow, PrefetchFlowConfig):
        pytest.fail(f"[profiles.{PROFILE}.flow] должен быть prefetch, получен {flow}")

    return flow


@pytest.fixture(scope="module")
def rephraser_config(flow_config: PrefetchFlowConfig) -> ChatSettings:
    """Секция переформулировщика; её отсутствие проверяется отдельным тестом."""
    if flow_config.rephraser is None:
        pytest.skip(f"[profiles.{PROFILE}.flow.rephraser] не задан")

    return flow_config.rephraser


@pytest.fixture(scope="module")
def call_stand() -> CallStand:
    """Держатель контекста модуля: инструменты собираются раз на модуль и читают
    контекст через него."""
    return CallStand()


@pytest.fixture(scope="module")
def runtime_stand(call_stand: CallStand) -> Iterator[StandRefs]:
    """Объекты процесса модуля поверх того же держателя контекста; способы
    запуска гасятся после модуля, как это делает выход приложения."""
    stand = StandRefs(call_stand.contexts)
    try:
        yield stand
    finally:
        stand.stop()


@pytest.fixture
async def chainlit_context(
    call_stand: CallStand, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """Сессия с ролями и профилем: их читают guard'ы доступа к инструментам.

    Профиль живёт на самой сессии: user_session перечитывает его оттуда при
    каждом обращении, поэтому положить значение в словарь мало.
    """
    from chainlit.context import init_http_context

    roles = sorted(app_config.roles)
    user = cl.User(identifier="flow-integration", metadata={"roles": roles})

    context = init_http_context(user=user)
    context.session.chat_profile = PROFILE
    call_stand.use(
        call_stand.context(
            thread_id="flow-integration",
            roles=roles,
            profile=PROFILE,
            login="flow-integration",
        )
    )
    yield
    call_stand.clear()


@pytest.fixture
async def session_tools(
    service_tools: ServiceTools, app_config: AppConfig
) -> SessionTools:
    """Инструменты сервиса boba-mcp и его порт от имени пользователя теста:
    тела исполняет сервис, граф видит их так же, как в приложении."""
    caller = McpCaller(login="flow-integration", roles=frozenset(app_config.roles))
    port = await service_tools.port_of(caller)

    return SessionTools(tools=list(port.tools()), service=port)


@pytest.fixture(scope="module")
async def providers(app_config: AppConfig) -> Any:
    """Модели профилей и их flow, как их держит приложение."""
    generator = llm_providers(app_config)
    built = await anext(generator)

    yield built

    await anext(generator, None)


def _graph(
    app_config: AppConfig,
    providers: LlmProviders,
    session: SessionTools,
) -> CompiledStateGraph:
    """Граф профиля: боевой билдер, память вместо postgres-checkpointer."""
    selected = SelectedProfile(name=PROFILE, config=app_config.profiles[PROFILE])
    settings = selected.config

    chat = ChatModelBridge(
        chat_model=providers.chat(settings),
        sampling=settings.sampling,
        model_name=settings.model,
    )

    names: list[str] = []
    for tool in session.tools:
        names.append(tool.name)

    spec = GraphSpec(
        chat=chat,
        service=session.service,
        system_prompt=settings.system_prompt,
        checkpoints=CheckpointMessages(InMemorySaver()),
        history=HistoryView(frozenset(names), settings.history_messages),
    )

    builder = session_graph_builder(providers, selected)
    if not isinstance(builder, PrefetchGraphBuilder):
        pytest.fail(f"профиль {PROFILE} должен строить prefetch-граф, а не {builder}")

    return builder.build(spec)


def _rephraser(
    rephraser: ChatSettings,
    providers: LlmProviders,
    **overrides: Any,
) -> LlmRephraser:
    """Переформулировщик профиля на его же модели; overrides подменяют секцию."""
    cfg = rephraser.model_copy(update=overrides)

    return LlmRephraser(
        SchemaReply(providers.chat(cfg), cfg.sampling), cfg.system_prompt
    )


def _prefetch_calls(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, AIMessage):
            continue

        for call in message.tool_calls:
            call_id = call["id"]
            if CallIdPrefix.PREFETCH.marks(call_id):
                calls.append(dict(call))

    return calls


def _tool_messages(messages: Sequence[BaseMessage]) -> list[ToolMessage]:
    replies: list[ToolMessage] = []
    for message in messages:
        if isinstance(message, ToolMessage):
            replies.append(message)

    return replies


class TestRephraser:
    """Переформулировщик на модели из конфига flow."""

    async def test_query_is_rephrased(
        self,
        rephraser_config: ChatSettings,
        providers: LlmProviders,
        chainlit_context: None,
    ) -> None:
        queries = await _rephraser(rephraser_config, providers).rephrase(QUESTION)

        if not queries:
            raise AssertionError("переформулировщик обязан дать хотя бы один запрос")

        if list(queries) == [QUESTION]:
            raise AssertionError("модель вернула исходный запрос: разбор не удался")

        for query in queries:
            if not query.strip():
                raise AssertionError(f"пустой запрос в выдаче: {queries}")

    async def test_unknown_model_searches_by_user_query(
        self,
        rephraser_config: ChatSettings,
        providers: LlmProviders,
        chainlit_context: None,
    ) -> None:
        """Модели нет у провайдера: поиск идёт по исходному запросу, ход живёт."""
        if not isinstance(rephraser_config.provider, OpenAiProvider):
            pytest.skip("подмена имени модели проверяется на удалённом провайдере")

        rephraser = _rephraser(
            rephraser_config, providers, model="no/such-model-at-all"
        )

        queries = await rephraser.rephrase(QUESTION)

        if list(queries) != [QUESTION]:
            raise AssertionError(f"откат на исходный запрос, получено {queries}")


class TestPrefetchGraph:
    """Полный ход профиля: подготовка контекста и ответ модели."""

    @pytest.mark.usefixtures("chainlit_context")
    async def test_turn_prefetches_and_answers(
        self,
        app_config: AppConfig,
        flow_config: PrefetchFlowConfig,
        rephraser_config: ChatSettings,
        providers: LlmProviders,
        session_tools: SessionTools,
    ) -> None:
        graph = _graph(app_config, providers, session_tools)

        result = await graph.ainvoke(
            {"messages": [HumanMessage(QUESTION)]}, config=THREAD
        )
        messages = result["messages"]

        calls = _prefetch_calls(messages)
        tools_count = len(flow_config.tools)
        if not calls:
            raise AssertionError("подготовка обязана вызвать поиск")

        if len(calls) % tools_count != 0:
            raise AssertionError(
                f"каждый запрос идёт в каждый инструмент, получено {len(calls)} "
                f"вызовов на {tools_count} инструмента"
            )

        expected = len(calls)

        called = set()
        for call in calls:
            called.add(call["name"])
        if called != set(flow_config.tools):
            raise AssertionError(f"вызваны не те инструменты: {called}")

        replies = _tool_messages(messages)
        if len(replies) < expected:
            raise AssertionError(f"ждём {expected} ответов, получено {len(replies)}")

        for reply in replies[:expected]:
            if reply.status == "error":
                raise AssertionError(f"поиск ответил ошибкой: {reply.content}")

            artifact = ToolArtifact.revive(reply.artifact)
            if not isinstance(artifact, TableResult):
                raise AssertionError(f"ждём таблицу попаданий, получено {artifact}")

        answer = messages[-1]
        if not isinstance(answer, AIMessage):
            raise AssertionError(f"последнее сообщение не ответ модели: {answer}")
        if not str(answer.content).strip():
            raise AssertionError("модель ответила пустотой")

    async def test_second_turn_is_prefetched_too(
        self,
        app_config: AppConfig,
        providers: LlmProviders,
        session_tools: SessionTools,
        chainlit_context: None,
    ) -> None:
        """Каждый вопрос треда обогащается своим поиском."""
        graph = _graph(app_config, providers, session_tools)

        first = await graph.ainvoke(
            {"messages": [HumanMessage(QUESTION)]}, config=THREAD
        )
        before = len(_prefetch_calls(first["messages"]))

        second = await graph.ainvoke(
            {"messages": [HumanMessage("а если коротко?")]}, config=THREAD
        )
        after = len(_prefetch_calls(second["messages"]))

        if before == 0:
            raise AssertionError("первый ход обязан готовить контекст")

        if after <= before:
            raise AssertionError(
                f"второй ход обязан добрать контекст: было {before}, стало {after}"
            )

    async def test_turn_without_rephraser_searches_by_user_query(
        self,
        app_config: AppConfig,
        flow_config: PrefetchFlowConfig,
        providers: LlmProviders,
        session_tools: SessionTools,
        chainlit_context: None,
    ) -> None:
        """Профиль без секции rephraser ищет по самому запросу пользователя."""
        profile = app_config.profiles[PROFILE].model_copy(
            update={"flow": flow_config.model_copy(update={"rephraser": None})}
        )
        config = app_config.model_copy(
            update={"profiles": {**app_config.profiles, PROFILE: profile}}
        )

        generator = llm_providers(config)
        plain_providers = await anext(generator)
        try:
            graph = _graph(config, plain_providers, session_tools)
            result = await graph.ainvoke(
                {"messages": [HumanMessage(QUESTION)]}, config=THREAD
            )
        finally:
            await anext(generator, None)

        calls = _prefetch_calls(result["messages"])
        if len(calls) != len(flow_config.tools):
            raise AssertionError(
                f"один запрос в каждый инструмент, получено {len(calls)}"
            )

        asked = set()
        for call in calls:
            asked.add(call["args"]["query"])
        if asked != {QUESTION}:
            raise AssertionError(f"в поиск ушёл не исходный запрос: {asked}")

        answer = result["messages"][-1]
        if not isinstance(answer, AIMessage):
            raise AssertionError(f"последнее сообщение не ответ модели: {answer}")
        if not str(answer.content).strip():
            raise AssertionError("модель ответила пустотой")

    @pytest.mark.usefixtures("providers", "chainlit_context")
    async def test_unreachable_provider_searches_by_user_query(
        self,
        app_config: AppConfig,
        flow_config: PrefetchFlowConfig,
        rephraser_config: ChatSettings,
        session_tools: SessionTools,
    ) -> None:
        """Переформулировщик недоступен: ход отвечает, поиск идёт по запросу."""
        provider = rephraser_config.provider
        if not isinstance(provider, OpenAiProvider):
            pytest.skip("недоступный endpoint проверяется на удалённом провайдере")

        dead_end = HttpConnection(
            scheme=UrlScheme.HTTP,
            host="127.0.0.1",
            port=9,
            path="/v1",
            auth=provider.connection.auth,
            retry_attempts=1,
        )
        unreachable = provider.model_copy(update={"connection": dead_end})
        rephraser = rephraser_config.model_copy(update={"provider": unreachable})
        broken = flow_config.model_copy(update={"rephraser": rephraser})
        profile = app_config.profiles[PROFILE].model_copy(update={"flow": broken})
        config = app_config.model_copy(
            update={"profiles": {**app_config.profiles, PROFILE: profile}}
        )

        providers_gen = llm_providers(config)
        broken_providers = await anext(providers_gen)
        try:
            graph = _graph(config, broken_providers, session_tools)
            result = await graph.ainvoke(
                {"messages": [HumanMessage(QUESTION)]}, config=THREAD
            )
        finally:
            await anext(providers_gen, None)

        asked = set()
        for call in _prefetch_calls(result["messages"]):
            asked.add(call["args"]["query"])

        if asked != {QUESTION}:
            raise AssertionError(f"в поиск ушёл не исходный запрос: {asked}")
