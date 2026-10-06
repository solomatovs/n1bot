"""Аварийная ошибка инструмента -> ErrorResult: ход не прерывается, LLM видит ошибку."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest
from pydantic import BaseModel, JsonValue

from boba.sandbox.zygote import ZygoteCallError
from boba.stand.toolstand import ProbeTools
from boba.toolkit.dag import NodeOutcome
from boba.toolkit.result import ErrorResult, ExceptionResult, MarkdownResult
from boba.toolrun.errors import ToolErrorGuard
from boba.toolrun.hosted import HostedTool

__all__: list[str] = []


class _BoomError(Exception):
    """исключение инструмента, которое должно превратиться в ErrorResult"""


class TextArgs(BaseModel):
    """Аргументы успешного инструмента."""

    text: str


class NoArgs(BaseModel):
    """Инструмент без аргументов."""


async def good(text: str) -> MarkdownResult:
    """успешный инструмент"""
    return MarkdownResult(text=text)


async def boom() -> ErrorResult:
    """инструмент, падающий аварийно (как oom killer песочницы)"""
    raise ZygoteCallError("doc:read_document: killed by OOM")


def _guarded() -> list[HostedTool]:
    probes = ProbeTools()
    tools = [
        probes.hosted("good", TextArgs, good),
        probes.hosted("boom", NoArgs, boom),
    ]

    return ToolErrorGuard().guard_all(tools)


class TestToolErrorGuard:
    @staticmethod
    def _invoke(tool: HostedTool, args: Mapping[str, JsonValue]) -> NodeOutcome:
        return asyncio.run(ProbeTools().call(tool, "c1", args))

    @staticmethod
    def test_ok_passes_through() -> None:
        g, _ = _guarded()
        message = TestToolErrorGuard._invoke(g, {"text": "hi"})
        if message.content != "hi":
            raise AssertionError('message.content == "hi"')
        if message.artifact != MarkdownResult(text="hi"):
            raise AssertionError('message.artifact == MarkdownResult(text="hi")')

    @staticmethod
    def test_raised_exception_becomes_error_result() -> None:
        _, b = _guarded()
        message = TestToolErrorGuard._invoke(b, {})
        artifact = message.artifact
        if not (isinstance(artifact, ExceptionResult)):
            raise AssertionError(f"artifact: {artifact!r}")
        if artifact.ok is not False:
            raise AssertionError("artifact.ok is False")
        if artifact.error_kind != "ZygoteCallError":
            raise AssertionError('artifact.error_kind == "ZygoteCallError"')
        if "OOM" not in message.content:
            raise AssertionError('"OOM" in message.content')

    @staticmethod
    def test_async_raised_exception_becomes_error_result() -> None:
        _, b = _guarded()

        message = asyncio.run(ProbeTools().call(b, "c2", {}))
        artifact = message.artifact
        if not (isinstance(artifact, ExceptionResult)):
            raise AssertionError(f"artifact: {artifact!r}")
        if artifact.ok is not False:
            raise AssertionError("artifact.ok is False")
        if "OOM" not in message.content:
            raise AssertionError('"OOM" in message.content')

    @staticmethod
    def test_base_exception_is_not_caught() -> None:
        """ToolStopped (отмена хода) должен прерывать, а не становиться ошибкой."""

        async def stopped() -> ErrorResult:
            raise KeyboardInterrupt

        probe = ProbeTools().hosted("stopped", NoArgs, stopped)
        (g,) = ToolErrorGuard().guard_all([probe])
        with pytest.raises(KeyboardInterrupt):
            TestToolErrorGuard._invoke(g, {})
