"""Аварийная ошибка инструмента -> ErrorResult: ход не прерывается, LLM видит ошибку."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool

from boba.chainlit.agent.tools.errors import ToolErrorGuard
from boba.toolkit.result import ErrorResult, TextResult
from boba.toolkit.sandbox.payload import SandboxPayloadError

__all__: list[str] = []


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """независимость от сессии chainlit"""


class _Boom(Exception):
    """исключение инструмента, которое должно превратиться в ErrorResult"""


@tool(response_format="content_and_artifact")
def good(text: str) -> tuple[str, TextResult]:
    """успешный инструмент"""
    return text, TextResult(text=text)


@tool(response_format="content_and_artifact")
def boom() -> tuple[str, ErrorResult]:
    """инструмент, падающий аварийно (как oom killer песочницы)"""
    raise SandboxPayloadError("doc:read_document: killed by OOM")


def _guarded() -> list:
    return ToolErrorGuard.guard_all([good, boom])


class TestToolErrorGuard:
    @staticmethod
    def _invoke(tool: BaseTool, args: Mapping[str, object]) -> ToolMessage:
        return tool.invoke(
            {"name": tool.name, "args": args, "id": "c1", "type": "tool_call"}
        )

    @staticmethod
    def test_ok_passes_through() -> None:
        g, _ = _guarded()
        message = TestToolErrorGuard._invoke(g, {"text": "hi"})
        assert message.content == "hi"
        assert message.artifact == TextResult(text="hi")

    @staticmethod
    def test_raised_exception_becomes_error_result() -> None:
        _, b = _guarded()
        message = TestToolErrorGuard._invoke(b, {})
        artifact = message.artifact
        assert isinstance(artifact, ErrorResult)
        assert artifact.ok is False
        assert artifact.error_kind == "SandboxPayloadError"
        assert "OOM" in message.content

    @staticmethod
    def test_async_raised_exception_becomes_error_result() -> None:
        _, b = _guarded()

        async def _call() -> ToolMessage:
            return await b.ainvoke(
                {"name": b.name, "args": {}, "id": "c2", "type": "tool_call"}
            )

        message = asyncio.run(_call())
        artifact = message.artifact
        assert isinstance(artifact, ErrorResult)
        assert artifact.ok is False
        assert "OOM" in message.content

    @staticmethod
    def test_base_exception_is_not_caught() -> None:
        """ToolStopped (отмена хода) должен прерывать, а не становиться ошибкой."""

        @tool
        def stopped() -> None:
            """прерываемый инструмент"""
            raise KeyboardInterrupt

        (g,) = ToolErrorGuard.guard_all([stopped])
        with pytest.raises(KeyboardInterrupt):
            TestToolErrorGuard._invoke(g, {})
