"""Вызов инструмента с неверными аргументами внутри хода (pytest -m integration).

Инструменты собираются боевым StandRefs.registry и работают в зиготе; модель —
по сценарию: первый вызов без обязательного аргумента, второй правильный,
затем ответ. Ход не прерывается: отказ валидации ложится в историю
сообщением инструмента со статусом error, модель его видит и повторяет вызов.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chainlit as cl
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from omegaconf import DictConfig, OmegaConf

from boba.chainlit.agent.flow import GraphSpec, PlainGraphBuilder
from boba.chainlit.infra.config import AppConfig
from boba.chainlit.infra.providers import build_history_view
from boba.stand.refs import StandRefs
from boba.stand_core.context import CallStand
from boba.toolkit.result import ErrorResult, ToolArtifact
from boba.toolrun.stream_calls import ToolServer

_REPO = Path(__file__).resolve().parents[4]
_SANDBOX_STAGING = _REPO / "build" / "src" / "sandbox"
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
TOOL = "kb_fts_search"

THREAD = RunnableConfig(configurable={"thread_id": "args-validation"})

BAD_CALL: dict[str, Any] = {
    "name": TOOL,
    "args": {},
    "id": "call-missing-query",
    "type": "tool_call",
}
GOOD_CALL: dict[str, Any] = {
    "name": TOOL,
    "args": {"query": "kerberos", "intent": "retry with the query filled in"},
    "id": "call-with-query",
    "type": "tool_call",
}
FINAL_ANSWER = "answered after retry"


class ScriptedChat(GenericFakeChatModel):
    """Модель по сценарию: bind_tools у фейка не реализован."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


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
    """Сессия с ролями и профилем: их читают guard'ы доступа к инструментам."""
    from chainlit.context import init_http_context

    roles = sorted(app_config.roles)
    user = cl.User(identifier="args-validation", metadata={"roles": roles})

    context = init_http_context(user=user)
    context.session.chat_profile = PROFILE
    call_stand.use(
        call_stand.context(
            thread_id="args-validation",
            roles=roles,
            profile=PROFILE,
            login="args-validation",
        )
    )
    yield
    call_stand.clear()


@dataclass(frozen=True)
class SessionTools:
    """Инструменты сессии и порт инструментов над ними, как их отдаёт реестр."""

    tools: list[BaseTool]
    service: ToolServer


@pytest.fixture(scope="module")
def session_tools(
    runtime_stand: StandRefs,
    call_stand: CallStand,
    service_raw_config: DictConfig,
    app_config: AppConfig,
) -> SessionTools:
    """Инструменты профиля, собранные боевым загрузчиком из конфига сервиса,
    который их исполняет, и их порт. Набор профиля — набор endpoint'а сервиса
    с тем же именем."""
    endpoint = OmegaConf.select(service_raw_config, f"mcp.endpoints.{PROFILE}.tools")
    registry = runtime_stand.registry(
        service_raw_config,
        runtime_stand.none(),
        StandRefs.granted(PROFILE, list(endpoint)),
    )
    roles = frozenset(app_config.roles)
    tools = registry.for_session(roles, PROFILE)

    return SessionTools(tools=tools, service=registry.server(tools))


def _graph(
    app_config: AppConfig,
    session: SessionTools,
    scripted: Sequence[AIMessage],
) -> CompiledStateGraph:
    """Граф профиля на модели по сценарию: боевой билдер, память вместо postgres."""
    settings = app_config.profiles[PROFILE]

    chat = ScriptedChat(messages=iter(list(scripted)), disable_streaming=True)

    names: list[str] = []
    for tool in session.tools:
        names.append(tool.name)

    spec = GraphSpec(
        chat=chat,
        service=session.service,
        system_prompt=settings.system_prompt,
        checkpointer=InMemorySaver(),
        history=build_history_view(frozenset(names), settings.history_messages),
    )

    return PlainGraphBuilder().build(spec)


def _replies(messages: Sequence[BaseMessage]) -> dict[str, ToolMessage]:
    by_call: dict[str, ToolMessage] = {}
    for message in messages:
        if isinstance(message, ToolMessage):
            by_call[message.tool_call_id] = message

    return by_call


def _index_of(messages: Sequence[BaseMessage], call_id: str) -> int:
    for index, message in enumerate(messages):
        if not isinstance(message, AIMessage):
            continue

        for call in message.tool_calls:
            if call["id"] == call_id:
                return index

    raise AssertionError(f"no assistant message carries tool call {call_id!r}")


class TestInvalidArguments:
    """Неверные аргументы — отказ до тела, ход продолжается повтором."""

    @pytest.mark.usefixtures("chainlit_context")
    async def test_validation_error_is_reported_and_turn_goes_on(
        self, app_config: AppConfig, session_tools: SessionTools
    ) -> None:
        if TOOL not in [tool.name for tool in session_tools.tools]:
            pytest.fail(f"{TOOL} is not among tools of profile {PROFILE}")

        scripted = [
            AIMessage(content="", tool_calls=[BAD_CALL]),
            AIMessage(content="", tool_calls=[GOOD_CALL]),
            AIMessage(content=FINAL_ANSWER),
        ]
        graph = _graph(app_config, session_tools, scripted)

        result = await graph.ainvoke(
            {"messages": [HumanMessage("найди про kerberos")]}, config=THREAD
        )
        messages = result["messages"]
        replies = _replies(messages)

        bad = replies.get(BAD_CALL["id"])
        if bad is None:
            raise AssertionError("history has no tool message for the invalid call")
        if bad.status != "error":
            raise AssertionError(f"invalid call must be an error reply, got {bad!r}")

        text = str(bad.content)
        if "query" not in text:
            raise AssertionError(f"error reply does not name the missing field: {text}")
        if "required" not in text.lower():
            raise AssertionError(
                f"error reply does not say the field is required: {text[:200]}"
            )

        good = replies.get(GOOD_CALL["id"])
        if good is None:
            raise AssertionError("history has no tool message for the retried call")
        if good.status == "error":
            raise AssertionError(f"retried call failed: {good.content}")

        artifact = ToolArtifact.revive(good.artifact)
        if isinstance(artifact, ErrorResult):
            raise AssertionError(f"retried call returned error artifact: {artifact}")

        bad_index = _index_of(messages, BAD_CALL["id"])
        good_index = _index_of(messages, GOOD_CALL["id"])
        if not (bad_index < messages.index(bad) < good_index):
            raise AssertionError("the model retried before seeing the error reply")

        last = messages[-1]
        if not isinstance(last, AIMessage) or last.content != FINAL_ANSWER:
            raise AssertionError(f"turn did not end with the final answer: {last!r}")
