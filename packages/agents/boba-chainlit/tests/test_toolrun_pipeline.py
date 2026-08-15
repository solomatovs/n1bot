"""Конвейер обёрток целиком: ToolCall-конверт -> обёртки -> тело функции.

Сборка повторяет загрузчик: ProcessWrap (локальный режим) -> InjectedConfig
-> ToolCallIdField -> ToolRunLogger; вызов — ainvoke полным ToolCall, как
зовёт ToolNode агента.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, ClassVar

import pytest
from langchain_core.tools import InjectedToolArg, tool
from pydantic import BaseModel, Field, SecretStr

from boba.chainlit.agent.toolrun.call_id import ToolCallIdField
from boba.chainlit.agent.toolrun.injected import InjectedConfig
from boba.chainlit.agent.toolrun.run_log import ToolRunLogger
from boba.chainlit.rendering.result import MarkdownRendering, ToolResultView
from boba.toolkit.entry import ToolMain
from boba.toolkit.launcher import PayloadFailureError
from boba.toolkit.result import (
    PgCopyTextResult,
    TextResult,
    ToolArtifact,
    ToolResult,
    render_for_llm,
)
from boba.toolkit.wrap import ToolProcessWrap


class PipeConfig(BaseModel):
    SECTION: ClassVar[str] = "tool.pipe"

    token: SecretStr


class PipeDownError(Exception):
    """Ожидаемый отказ конвейерного фейка."""


class PipeErrorKind(StrEnum):
    DOWN = "pipe_down"


EXPECTED: Mapping[type[Exception], PipeErrorKind] = {
    PipeDownError: PipeErrorKind.DOWN,
}


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def build_pipeline() -> Any:
    """Свежий инструмент, обёрнутый как в загрузчике."""

    @tool(response_format="content_and_artifact")
    async def pipe_echo(
        text: Annotated[str, Field(min_length=1, description="Что вернуть")],
        cfg: Annotated[PipeConfig, InjectedToolArg],
    ) -> tuple[str, ToolResult]:
        """Возвращает текст с секретом конфига."""
        if text == "boom":
            msg = "pipe backend is down"
            raise PipeDownError(msg)

        artifact = TextResult(text=f"{text}|{cfg.token.get_secret_value()}")
        return render_for_llm(artifact), artifact

    # EXPECTED ищется по модулю тела — у теста это модуль этого файла
    ToolProcessWrap.guard_all(ToolMain.toolset(pipe_echo), None)
    InjectedConfig.bind_all(
        [pipe_echo],
        lambda name, annotation: PipeConfig(token=SecretStr("p1p3")),
    )
    ToolCallIdField.attach_all([pipe_echo])
    ToolRunLogger.guard_all([pipe_echo], lambda tool, call_id: None)

    return pipe_echo


def call_envelope(text: str) -> dict[str, Any]:
    return {
        "name": "pipe_echo",
        "args": {"text": text},
        "id": "call-pipe-1",
        "type": "tool_call",
    }


class TestPipeline:
    def test_llm_schema_hides_injected_and_call_id(self) -> None:
        pipe_echo = build_pipeline()

        schema = pipe_echo.tool_call_schema
        assert list(schema.model_fields) == ["text"]

    def test_tool_call_invocation_reaches_the_body(self) -> None:
        pipe_echo = build_pipeline()

        message = asyncio.run(pipe_echo.ainvoke(call_envelope("hi")))

        assert "hi|p1p3" in str(message.content)

    def test_expected_error_kind_survives_the_pipeline(self) -> None:
        pipe_echo = build_pipeline()

        with pytest.raises(PayloadFailureError) as caught:
            asyncio.run(pipe_echo.ainvoke(call_envelope("boom")))

        assert caught.value.kind == "pipe_down"
        assert "pipe backend is down" in str(caught.value)


class TestArtifactRendering:
    """2c: артефакт нового пути оживает и рендерится существующим механизмом."""

    def test_artifact_from_pipeline_revives_and_renders(self) -> None:
        pipe_echo = build_pipeline()

        message = asyncio.run(pipe_echo.ainvoke(call_envelope("hi")))

        revived = ToolArtifact.revive(message.artifact)
        assert isinstance(revived, TextResult)

        rendering = ToolResultView(revived).render()
        assert isinstance(rendering, MarkdownRendering)
        assert "hi|p1p3" in rendering.markdown

    def test_serialized_artifact_revives_from_history(self) -> None:
        """История хранит артефакт сериализованным dict'ом (langgraph)."""
        pipe_echo = build_pipeline()

        message = asyncio.run(pipe_echo.ainvoke(call_envelope("hi")))
        stored = message.artifact.model_dump(mode="json")

        revived = ToolArtifact.revive(stored)
        assert isinstance(revived, TextResult)
        assert "hi|p1p3" in revived.text

    def test_pg_copy_artifact_renders_as_table(self) -> None:
        """Артефакт пилота: kind pg_copy_text рисуется таблицей, не JSON."""
        artifact = PgCopyTextResult(text="n\n1\n")

        revived = ToolArtifact.revive(artifact.model_dump(mode="json"))
        assert isinstance(revived, PgCopyTextResult)

        rendering = ToolResultView(revived).render()
        assert isinstance(rendering, MarkdownRendering)
        assert "| n" in rendering.markdown
