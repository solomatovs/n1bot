"""Сбой не должен быть тихим: фоновые таски chainlit и коллбэки langchain."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import uuid4

import pytest
from chainlit.context import ChainlitContext
from langchain_core.outputs import LLMResult
from langchain_core.tracers.base import AsyncBaseTracer

from boba.chainlit.chat import tracing as tracer_module
from boba.chainlit.chat.tracing import AgentTracer
from boba.chainlit.chat.turn import TurnState
from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.chainlit.data.errors import DataLayerError
from boba.chainlit.rendering.chat_view import ChatView


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
    AsyncBaseTracer.__init__(tracer)
    tracer._context = cast(ChainlitContext, None)
    tracer._view = cast(ChatView, _BrokenView())
    tracer._state = TurnState()
    return tracer


class TestTracerFailuresVisible:
    """langchain гасит исключения коллбэков: трасер обязан показать их сам."""

    def test_tool_start_failure_shown(self, shown: list[str]) -> None:
        tracer = _tracer()
        asyncio.run(tracer.on_tool_start({}, "", run_id=uuid4(), inputs={}))
        if not (shown):
            raise AssertionError("shown")
        if "on_tool_start" not in shown[0]:
            raise AssertionError('"on_tool_start" in shown[0]')

    def test_llm_end_failure_shown(self, shown: list[str]) -> None:
        tracer = _tracer()
        run_id = uuid4()
        tracer._state.add_reasoning(str(run_id), "мысли")

        async def _run() -> None:
            await tracer.on_llm_start({}, [""], run_id=run_id)
            await tracer.on_llm_end(LLMResult(generations=[]), run_id=run_id)

        asyncio.run(_run())
        if not (shown):
            raise AssertionError("shown")
        if "on_llm_end" not in shown[0]:
            raise AssertionError('"on_llm_end" in shown[0]')

    def test_failure_does_not_break_the_turn(self, shown: list[str]) -> None:
        tracer = _tracer()
        result = asyncio.run(tracer.on_tool_start({}, "", run_id=uuid4(), inputs={}))
        if result is not None:
            raise AssertionError("result is None")
        if not (shown):
            raise AssertionError("shown")


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
