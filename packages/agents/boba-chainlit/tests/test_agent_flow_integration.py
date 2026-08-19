"""Prefetch-flow целиком на боевом конфиге (pytest -m integration).

Граф собирается той же цепочкой провайдеров, что и в приложении: инструменты
приходят из load_tools и работают в песочнице, переформулировщик и основная
модель ходят к провайдеру из конфига.

Cgroup-лимиты сняты: pytest живёт вне делегированного cgroup.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from omegaconf import DictConfig
from pydantic import SecretStr

import chainlit as cl
from boba.chainlit.agent.chat_model import ReasoningChatOpenAI
from boba.chainlit.agent.flow import (
    GraphSpec,
    LlmRephraser,
    PrefetchCall,
    PrefetchError,
    PrefetchGraphBuilder,
)
from boba.chainlit.infra.config import (
    AppConfig,
    PrefetchFlowConfig,
    RephraserConfig,
    SelectedProfile,
)
from boba.chainlit.infra.plugins import load_tools
from boba.chainlit.infra.providers import (
    _graph_builder,
    build_history_view,
    httpx_clients,
    httpx_timeout,
)
from boba.settings import bind
from boba.toolkit.result import TableResult, ToolArtifact

_REPO = Path(__file__).resolve().parents[4]
_ROOTFS = _REPO / "build" / "src" / "sandbox" / "rootfs"

_CGROUP_BASE = os.environ.get("BOBA_CGROUP_BASE", "/sys/fs/cgroup/boba")


def _cgroup_delegated() -> bool:
    base_ok = os.access(os.path.join(_CGROUP_BASE, "cgroup.procs"), os.W_OK)
    root_ok = os.access("/sys/fs/cgroup/cgroup.procs", os.W_OK)
    return base_ok and root_ok


pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(
        shutil.which("bwrap") is None or not (_ROOTFS / "bin" / "sh").exists(),
        reason="нет bwrap или артефактов песочницы (собрать: make deps)",
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
def app_config(raw_config: DictConfig) -> AppConfig:
    return bind(raw_config, path="app", model=AppConfig)


@pytest.fixture(scope="module")
def flow_config(app_config: AppConfig) -> PrefetchFlowConfig:
    """Секция flow боевого профиля; без неё проверять нечего."""
    flow = app_config.profiles[PROFILE].flow
    if not isinstance(flow, PrefetchFlowConfig):
        pytest.fail(f"[profiles.{PROFILE}.flow] должен быть prefetch, получен {flow}")

    return flow


@pytest.fixture(scope="module")
def rephraser_config(flow_config: PrefetchFlowConfig) -> RephraserConfig:
    """Секция переформулировщика; её отсутствие проверяется отдельным тестом."""
    if flow_config.rephraser is None:
        pytest.skip(f"[profiles.{PROFILE}.flow.rephraser] не задан")

    return flow_config.rephraser


@pytest.fixture
async def chainlit_context(app_config: AppConfig) -> AsyncIterator[None]:
    """Сессия с ролями и профилем: их читают guard'ы доступа к инструментам.

    Профиль живёт на самой сессии: user_session перечитывает его оттуда при
    каждом обращении, поэтому положить значение в словарь мало.
    """
    from chainlit.context import init_http_context

    roles = sorted(app_config.roles)
    user = cl.User(identifier="flow-integration", metadata={"roles": roles})

    context = init_http_context(user=user)
    context.session.chat_profile = PROFILE

    yield


@pytest.fixture(scope="module")
def session_tools(raw_config: DictConfig, app_config: AppConfig) -> list[BaseTool]:
    """Инструменты профиля, собранные боевым загрузчиком."""
    registry = load_tools(raw_config)
    roles = frozenset(app_config.roles)
    return registry.for_session(roles, PROFILE)


@pytest.fixture(scope="module")
async def clients(app_config: AppConfig) -> Any:
    """HTTP-клиенты профилей и их flow, как их держит приложение."""
    generator = httpx_clients(app_config)
    built = await anext(generator)

    yield built

    await anext(generator, None)


def _graph(
    app_config: AppConfig,
    clients: dict[str, AsyncClient],
    tools: Sequence[BaseTool],
) -> CompiledStateGraph:
    """Граф профиля: боевой билдер, память вместо postgres-checkpointer."""
    selected = SelectedProfile(name=PROFILE, config=app_config.profiles[PROFILE])
    settings = selected.config

    chat = ReasoningChatOpenAI(
        http_async_client=clients[PROFILE],
        model=settings.model,
        base_url=settings.provider.base_url,
        api_key=SecretStr(settings.provider.api_key),
        timeout=httpx_timeout(settings.provider),
        max_retries=settings.provider.max_retries,
        **settings.chat_kwargs(),
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

    builder = _graph_builder(selected, clients, tools)
    if not isinstance(builder, PrefetchGraphBuilder):
        pytest.fail(f"профиль {PROFILE} должен строить prefetch-граф, а не {builder}")

    return builder.build(spec)


def _rephraser(
    rephraser: RephraserConfig,
    clients: dict[str, AsyncClient],
    **overrides: Any,
) -> LlmRephraser:
    """Переформулировщик профиля; overrides подменяют поля его секции."""
    cfg = rephraser.model_copy(update=overrides)

    chat = ReasoningChatOpenAI(
        http_async_client=clients[PrefetchFlowConfig.client_key(PROFILE)],
        model=cfg.model,
        base_url=cfg.provider.base_url,
        api_key=SecretStr(cfg.provider.api_key),
        timeout=httpx_timeout(cfg.provider),
        max_retries=0,
        disable_streaming=True,
        **cfg.chat_kwargs(),
    )

    return LlmRephraser(chat, cfg.system_prompt, cfg.queries)


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
        rephraser_config: RephraserConfig,
        clients: dict[str, AsyncClient],
        chainlit_context: None,
    ) -> None:
        queries = await _rephraser(rephraser_config, clients).rephrase(QUESTION)

        if len(queries) != rephraser_config.queries:
            raise AssertionError(
                f"ждём {rephraser_config.queries} запросов, получено "
                f"{len(queries)}: {queries}"
            )

        for query in queries:
            if not query.strip():
                raise AssertionError(f"пустой запрос в выдаче: {queries}")

    async def test_unknown_model_fails(
        self,
        rephraser_config: RephraserConfig,
        clients: dict[str, AsyncClient],
        chainlit_context: None,
    ) -> None:
        """Модели нет у провайдера: отказ виден, а не превращается в пустоту."""
        rephraser = _rephraser(
            rephraser_config, clients, model="no/such-model-at-all"
        )

        with pytest.raises(Exception) as caught:
            await rephraser.rephrase(QUESTION)

        if isinstance(caught.value, PrefetchError):
            return

        text = str(caught.value)
        if "no/such-model-at-all" not in text and "model" not in text.lower():
            raise AssertionError(f"отказ провайдера без причины: {text[:200]}")


class TestPrefetchGraph:
    """Полный ход профиля: подготовка контекста и ответ модели."""

    async def test_turn_prefetches_and_answers(
        self,
        app_config: AppConfig,
        flow_config: PrefetchFlowConfig,
        rephraser_config: RephraserConfig,
        clients: dict[str, AsyncClient],
        session_tools: list[BaseTool],
        chainlit_context: None,
    ) -> None:
        graph = _graph(app_config, clients, session_tools)

        result = await graph.ainvoke(
            {"messages": [HumanMessage(QUESTION)]}, config=THREAD
        )
        messages = result["messages"]

        calls = _prefetch_calls(messages)
        expected = rephraser_config.queries * len(flow_config.tools)
        if len(calls) != expected:
            raise AssertionError(f"ждём {expected} вызовов, получено {len(calls)}")

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
        clients: dict[str, AsyncClient],
        session_tools: list[BaseTool],
        chainlit_context: None,
    ) -> None:
        """Каждый вопрос треда обогащается своим поиском."""
        graph = _graph(app_config, clients, session_tools)

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
        clients: dict[str, AsyncClient],
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

        generator = httpx_clients(config)
        plain_clients = await anext(generator)
        try:
            if PrefetchFlowConfig.client_key(PROFILE) in plain_clients:
                raise AssertionError("клиент переформулировщика не нужен")

            graph = _graph(config, plain_clients, session_tools)
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

    async def test_unreachable_provider_fails_the_turn(
        self,
        app_config: AppConfig,
        flow_config: PrefetchFlowConfig,
        rephraser_config: RephraserConfig,
        clients: dict[str, AsyncClient],
        session_tools: list[BaseTool],
        chainlit_context: None,
    ) -> None:
        """Переформулировщик недоступен: ход падает, а не отвечает без контекста."""
        rephraser = rephraser_config.model_copy(
            update={
                "provider": rephraser_config.provider.model_copy(
                    update={"base_url": "http://127.0.0.1:9/v1", "max_retries": 0}
                )
            }
        )
        broken = flow_config.model_copy(update={"rephraser": rephraser})
        profile = app_config.profiles[PROFILE].model_copy(update={"flow": broken})
        config = app_config.model_copy(
            update={"profiles": {**app_config.profiles, PROFILE: profile}}
        )

        generator = httpx_clients(config)
        broken_clients = await anext(generator)
        try:
            graph = _graph(config, broken_clients, session_tools)

            with pytest.raises(PrefetchError):
                await graph.ainvoke(
                    {"messages": [HumanMessage(QUESTION)]}, config=THREAD
                )
        finally:
            await anext(generator, None)
