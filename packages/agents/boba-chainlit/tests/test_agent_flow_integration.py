"""Prefetch-flow целиком на боевом конфиге (pytest -m integration).

Граф собирается той же цепочкой провайдеров, что и в приложении: инструменты
приходят из ChatPlugins.load и работают в песочнице, переформулировщик и основная
модель ходят к провайдеру из конфига.

Cgroup-лимиты сняты: pytest живёт вне делегированного cgroup.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import chainlit as cl
import pytest
from chainlit_stand import use_context
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from omegaconf import DictConfig

from boba.chainlit.agent.bridge import ChatModelBridge
from boba.chainlit.agent.flow import (
    GraphSpec,
    LlmRephraser,
    PrefetchCall,
    PrefetchGraphBuilder,
)
from boba.chainlit.infra.config import AppConfig
from boba.chainlit.infra.plugins import ChatPlugins
from boba.chainlit.infra.providers import (
    build_history_view,
    llm_providers,
    session_graph_builder,
)
from boba.chat.profiles import ChatSettings, PrefetchFlowConfig, SelectedProfile
from boba.connection_broker.store import ConnectionStore
from boba.llm.http.openai import OpenAiProvider
from boba.llm.providers import LlmProviders
from boba.llm.schema import SchemaReply
from boba.stand.refs import StandRefs
from boba.toolkit.result import TableResult, ToolArtifact
from boba.transport.http.connection import HttpConnection, UrlScheme

_REPO = Path(__file__).resolve().parents[4]
_SANDBOX_STAGING = _REPO / "build" / "chainlit" / "src" / "sandbox"
_ROOTFS_IMAGE = _SANDBOX_STAGING / "plugins" / "boba-tool-shell" / "rootfs.ext4"

_CGROUP_BASE = os.environ.get("BOBA_CGROUP_BASE", "/sys/fs/cgroup/boba")


def _cgroup_delegated() -> bool:
    base_ok = os.access(os.path.join(_CGROUP_BASE, "cgroup.procs"), os.W_OK)
    root_ok = os.access("/sys/fs/cgroup/cgroup.procs", os.W_OK)
    return base_ok and root_ok


pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(
        shutil.which("bwrap") is None or not _ROOTFS_IMAGE.exists(),
        reason="нет bwrap или артефактов песочницы (собрать: make fetch sandbox)",
    ),
    pytest.mark.skipif(
        not _cgroup_delegated(),
        reason=f"cgroup base {_CGROUP_BASE} не делегирован пользователю",
    ),
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


@pytest.fixture
async def chainlit_context(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сессия с ролями и профилем: их читают guard'ы доступа к инструментам.

    Профиль живёт на самой сессии: user_session перечитывает его оттуда при
    каждом обращении, поэтому положить значение в словарь мало.
    """
    from chainlit.context import init_http_context

    roles = sorted(app_config.roles)
    user = cl.User(identifier="flow-integration", metadata={"roles": roles})

    context = init_http_context(user=user)
    context.session.chat_profile = PROFILE
    use_context(
        monkeypatch,
        thread_id="flow-integration",
        roles=roles,
        profile=PROFILE,
        login="flow-integration",
    )


def _no_registry() -> None:
    return None


def _no_store() -> ConnectionStore:
    msg = "the flow under test does not reach user connections"
    raise RuntimeError(msg)


@pytest.fixture(scope="module")
def session_tools(raw_config: DictConfig, app_config: AppConfig) -> list[BaseTool]:
    """Инструменты профиля, собранные боевым загрузчиком."""
    registry = ChatPlugins.load(raw_config, StandRefs.of(_no_store, _no_registry))
    roles = frozenset(app_config.roles)
    return registry.for_session(roles, PROFILE)


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
    tools: Sequence[BaseTool],
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
    for tool in tools:
        names.append(tool.name)

    spec = GraphSpec(
        chat=chat,
        tools=tools,
        system_prompt=settings.system_prompt,
        checkpointer=InMemorySaver(),
        history=build_history_view(frozenset(names), settings.history_messages),
    )

    builder = session_graph_builder(providers, selected, tools)
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
            if call_id and call_id.startswith(PrefetchCall.PREFIX):
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
        session_tools: list[BaseTool],
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
        session_tools: list[BaseTool],
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
        session_tools: list[BaseTool],
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
        session_tools: list[BaseTool],
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
