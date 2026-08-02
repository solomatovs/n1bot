"""Сбой не должен быть тихим: фоновые таски chainlit и коллбэки langchain."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar, cast
from uuid import uuid4

import pytest
from chainlit.context import ChainlitContext

from boba.chainlit2.chat import agent_tracer as tracer_module
from boba.chainlit2.chat.agent_tracer import AgentTracer
from boba.chainlit2.chat.data import data_layer as data_layer_module
from boba.chainlit2.chat.data.data_layer import PostgresDataLayer
from boba.chainlit2.rendering.chat_view import ChatView


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture
def shown(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    messages: list[str] = []

    async def fake_show(content: str, *args: Any, **kwargs: Any) -> None:
        messages.append(content)

    monkeypatch.setattr(tracer_module, "show_error", fake_show)
    monkeypatch.setattr(data_layer_module, "show_error", fake_show)
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
        self.content = None
        self.path = "/nonexistent/path/data.csv"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name}


def _tracer() -> AgentTracer:
    tracer = AgentTracer.__new__(AgentTracer)
    tracer._context = cast(ChainlitContext, None)
    tracer._view = cast(ChatView, _BrokenView())
    tracer._reasoning = {}
    tracer._tool_steps = {}
    return tracer


class TestTracerFailuresVisible:
    """langchain гасит исключения коллбэков: трасер обязан показать их сам."""

    def test_tool_start_failure_shown(self, shown: list[str]) -> None:
        tracer = _tracer()
        asyncio.run(tracer.on_tool_start({}, "", run_id=uuid4(), inputs={}))
        assert shown
        assert "on_tool_start" in shown[0]

    def test_llm_end_failure_shown(self, shown: list[str]) -> None:
        tracer = _tracer()
        run_id = uuid4()
        tracer._reasoning[str(run_id)] = "мысли"

        class _Response:
            generations: ClassVar[list[list[Any]]] = []

        asyncio.run(tracer.on_llm_end(_Response(), run_id=run_id))
        assert shown
        assert "on_llm_end" in shown[0]

    def test_failure_does_not_break_the_turn(self, shown: list[str]) -> None:
        tracer = _tracer()
        result = asyncio.run(
            tracer.on_tool_start({}, "", run_id=uuid4(), inputs={})
        )
        assert result is None
        assert shown


class TestDataLayerFailuresVisible:
    """create_element/delete_* chainlit зовёт фоновой таской: raise не виден."""

    @staticmethod
    def _layer() -> PostgresDataLayer:
        return PostgresDataLayer.__new__(PostgresDataLayer)

    def test_unreadable_attachment_shown(self, shown: list[str]) -> None:
        layer = self._layer()
        element = _Element(for_id=str(uuid4()))
        create = PostgresDataLayer.create_element.__wrapped__
        with pytest.raises(Exception, match="create_element"):
            asyncio.run(create(layer, element))
        assert shown
        assert "data.csv" in shown[0]

    def test_element_without_for_id_is_skipped(self, shown: list[str]) -> None:
        layer = self._layer()
        create = PostgresDataLayer.create_element.__wrapped__
        asyncio.run(create(layer, _Element(for_id=None)))
        assert not shown
