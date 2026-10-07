"""Сбой не должен быть тихим: коллбэки langchain, шаги вызовов инструментов и
слой данных."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Annotated, Any, cast
from uuid import uuid4

import pytest
from chainlit.context import ChainlitContext
from chainlit_stand import FakeThreadMessages
from langchain_core.outputs import LLMResult
from pydantic import Field

from boba.chainlit.agent.events import TurnEvents
from boba.chainlit.canvas.remote import RemoteJournals
from boba.chainlit.chat import tracing as tracer_module
from boba.chainlit.chat.feed import TurnFeed
from boba.chainlit.chat.tracing import AgentTracer
from boba.chainlit.chat.turn import TurnState
from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.chainlit.rendering.mount import ChatCalls, ChatMount
from boba.chat.threads import DataLayerError
from boba.connection_broker.sealing import SentConnections
from boba.identity.run import Runs
from boba.stand.refs import StandRefs
from boba.stand_core.context import CallStand, FakeTurn
from boba.toolkit.chain import CallAmbient
from boba.toolkit.dag import DagNode, NodeOutcome
from boba.toolkit.facade import tool
from boba.toolkit.ports import StreamSpecs
from boba.toolkit.result import MarkdownResult
from boba.toolrun.hosted import ToolHosting
from boba.toolrun.stream_calls import LocalDagService


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture
def shown(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    messages: list[str] = []

    async def fake_show(content: str, *args: Any, **kwargs: Any) -> None:
        messages.append(content)

    monkeypatch.setattr(tracer_module, "show_error", fake_show)
    return messages


class _BrokenView:
    """Любой вызов отрисовки падает — как при битом Plotly-спеке."""

    def __getattr__(self, name: str) -> Any:
        async def boom(*args: Any, **kwargs: Any) -> None:
            msg = f"отрисовка {name} сломана"
            raise RuntimeError(msg)

        return boom


class _Element:
    def __init__(self, for_id: str | None) -> None:
        self.for_id = for_id
        self.id = str(uuid4())
        self.thread_id = str(uuid4())
        self.name = "data.csv"
        self.mime = "text/csv"
        self.display = "inline"
        self.content = None
        self.path = "/nonexistent/path/data.csv"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name}


def _tracer() -> AgentTracer:
    tracer = AgentTracer.__new__(AgentTracer)
    tracer._context = cast(ChainlitContext, None)
    tracer._feed = cast(TurnFeed, _BrokenView())
    tracer._state = TurnState()
    return tracer


def _events(tracer: AgentTracer) -> TurnEvents:
    """Колбэк-обработчик хода: события langchain приходят трасеру через него."""
    return TurnEvents([tracer], tracer)


class TestTracerFailuresVisible:
    """langchain гасит исключения коллбэков: трасер обязан показать их сам."""

    def test_llm_end_failure_shown(self, shown: list[str]) -> None:
        tracer = _tracer()
        events = _events(tracer)
        run_id = uuid4()
        tracer._state.add_reasoning(str(run_id), "мысли")

        async def _run() -> None:
            await events.on_chat_model_start({}, [[]], run_id=run_id)
            await events.on_llm_end(LLMResult(generations=[]), run_id=run_id)

        asyncio.run(_run())
        if not (shown):
            raise AssertionError("shown")
        if "model_replied" not in shown[0]:
            raise AssertionError('"model_replied" in shown[0]')

    def test_failure_does_not_break_the_turn(self, shown: list[str]) -> None:
        events = _events(_tracer())
        run_id = uuid4()

        async def _run() -> None:
            await events.on_chat_model_start({}, [[]], run_id=run_id)

            return await events.on_llm_error(RuntimeError("provider"), run_id=run_id)

        result = asyncio.run(_run())
        if result is not None:
            raise AssertionError("result is None")
        if not (shown):
            raise AssertionError("shown")
        if "model_failed" not in shown[0]:
            raise AssertionError('"model_failed" in shown[0]')


class BrokenTurn(FakeTurn):
    """Владелец хода, у которого лента недоступна: начало вызова не рисуется."""

    async def tool_started(
        self, tool_call_id: str, name: str, args: Mapping[str, Any]
    ) -> None:
        msg = f"отрисовка шага {name} сломана"
        raise RuntimeError(msg)


class TestToolStepFailuresVisible:
    """Шаг вызова инструмента рисует не langchain: сбой ленты гасить некому,
    он уходит наверх ошибкой вызова, и ход показывает его своим отчётом."""

    def test_step_failure_goes_up_and_the_body_is_not_run(
        self, call_stand: CallStand
    ) -> None:
        ran: list[str] = []

        @tool
        async def step_probe(
            text: Annotated[str, Field(min_length=1, description="Что вернуть")],
        ) -> MarkdownResult:
            """Запоминает, что тело исполнилось."""
            ran.append(text)

            return MarkdownResult(text=text)

        contexts = call_stand.contexts
        runs = Runs(contexts)
        calls = ChatCalls(
            contexts,
            runs,
            SentConnections(),
            ChatMount(contexts, runs),
            RemoteJournals(FakeThreadMessages()),
        )
        service = LocalDagService(
            ToolHosting().toolset([step_probe]),
            StandRefs.STREAM_CONFIG,
            (),
            StreamSpecs({}),
            CallAmbient(),
            calls,
        )
        node = DagNode(key="call-1", tool="step_probe", args={"text": "hi"})

        async def called() -> NodeOutcome:
            with runs.open(call_stand.context("broken-feed"), BrokenTurn()):
                return await service.call(node)

        with pytest.raises(RuntimeError, match="отрисовка шага step_probe сломана"):
            asyncio.run(called())

        if ran:
            raise AssertionError(f"тело не исполняется без шага ленты: {ran}")


class TestDataLayerErrorContract:
    """Слой данных ничего не рисует: наружу уходит только его собственная ошибка."""

    @staticmethod
    def _layer() -> PostgresDataLayer:
        return PostgresDataLayer.__new__(PostgresDataLayer)

    def test_unreadable_attachment_becomes_layer_error(self, shown: list[str]) -> None:
        layer = self._layer()
        element = _Element(for_id=str(uuid4()))
        # __wrapped__ снимает обёртку chainlit, оставляя границу слоя данных
        create = PostgresDataLayer.create_element.__wrapped__

        with pytest.raises(DataLayerError) as failure:
            asyncio.run(create(layer, element))

        if "create_element" not in str(failure.value):
            raise AssertionError('"create_element" in str(failure.value)')
        if shown:
            raise AssertionError("not shown")

    def test_element_without_for_id_is_skipped(self, shown: list[str]) -> None:
        layer = self._layer()
        create = PostgresDataLayer.create_element.__wrapped__
        asyncio.run(create(layer, _Element(for_id=None)))
        if shown:
            raise AssertionError("not shown")
