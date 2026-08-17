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
from boba.toolkit.channels import JournalChannel
from boba.toolkit.entry import ToolMain
from boba.toolkit.launcher import PayloadFailureError
from boba.toolkit.result import (
    PgCopyTextResult,
    TextResult,
    ToolArtifact,
    ToolResult,
    render_for_llm,
)
from boba.toolkit.stream import ToolChannelsTap
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
        if list(schema.model_fields) != ["text"]:
            raise AssertionError('list(schema.model_fields) == ["text"]')

    def test_tool_call_invocation_reaches_the_body(self) -> None:
        pipe_echo = build_pipeline()

        message = asyncio.run(pipe_echo.ainvoke(call_envelope("hi")))

        if "hi|p1p3" not in str(message.content):
            raise AssertionError('"hi|p1p3" in str(message.content)')

    def test_expected_error_kind_survives_the_pipeline(self) -> None:
        pipe_echo = build_pipeline()

        with pytest.raises(PayloadFailureError) as caught:
            asyncio.run(pipe_echo.ainvoke(call_envelope("boom")))

        if caught.value.kind != "pipe_down":
            raise AssertionError('caught.value.kind == "pipe_down"')
        if "pipe backend is down" not in str(caught.value):
            raise AssertionError('"pipe backend is down" in str(caught.value)')


class TestArtifactRendering:
    """2c: артефакт нового пути оживает и рендерится существующим механизмом."""

    def test_artifact_from_pipeline_revives_and_renders(self) -> None:
        pipe_echo = build_pipeline()

        message = asyncio.run(pipe_echo.ainvoke(call_envelope("hi")))

        revived = ToolArtifact.revive(message.artifact)
        if not (isinstance(revived, TextResult)):
            raise AssertionError("isinstance(revived, TextResult)")

        rendering = ToolResultView(revived).render()
        if not (isinstance(rendering, MarkdownRendering)):
            raise AssertionError("isinstance(rendering, MarkdownRendering)")
        if "hi|p1p3" not in rendering.markdown:
            raise AssertionError('"hi|p1p3" in rendering.markdown')

    def test_serialized_artifact_revives_from_history(self) -> None:
        """История хранит артефакт сериализованным dict'ом (langgraph)."""
        pipe_echo = build_pipeline()

        message = asyncio.run(pipe_echo.ainvoke(call_envelope("hi")))
        stored = message.artifact.model_dump(mode="json")

        revived = ToolArtifact.revive(stored)
        if not (isinstance(revived, TextResult)):
            raise AssertionError("isinstance(revived, TextResult)")
        if "hi|p1p3" not in revived.text:
            raise AssertionError('"hi|p1p3" in revived.text')

    def test_pg_copy_artifact_renders_as_table(self) -> None:
        """Артефакт пилота: kind pg_copy_text рисуется таблицей, не JSON."""
        artifact = PgCopyTextResult(text="n\n1\n")

        revived = ToolArtifact.revive(artifact.model_dump(mode="json"))
        if not (isinstance(revived, PgCopyTextResult)):
            raise AssertionError("isinstance(revived, PgCopyTextResult)")

        rendering = ToolResultView(revived).render()
        if not (isinstance(rendering, MarkdownRendering)):
            raise AssertionError("isinstance(rendering, MarkdownRendering)")
        if "| n" not in rendering.markdown:
            raise AssertionError('"| n" in rendering.markdown')


class _FakeStream:
    """Журнал вызова для теста: приёмники каналов и заметка закрытия."""

    def __init__(self) -> None:
        self.fed: dict[JournalChannel, bytearray] = {}
        self.note = ""

    def sink_of(self, channel: JournalChannel) -> Any:
        buffer = self.fed.setdefault(channel, bytearray())

        class _Sink:
            def feed(self, data: bytes) -> None:
                buffer.extend(data)

            def feed_text(self, text: str) -> None:
                buffer.extend(text.encode())

        return _Sink()

    def close(self, note: str) -> None:
        self.note = note


class TestChannelTap:
    """ToolRunLogger обязан подключить приёмники каналов: без ToolChannelsTap
    канальный запуск не журналирует ни байта."""

    @pytest.mark.anyio
    async def test_channels_tap_is_set_during_the_call(self) -> None:
        stream = _FakeStream()
        seen: list[Any] = []

        @tool(response_format="content_and_artifact")
        async def tap_probe(
            text: Annotated[str, Field(min_length=1, description="Что вернуть")],
        ) -> tuple[str, ToolResult]:
            """Фиксирует, какие тапы видит тело во время вызова."""
            seen.append(ToolChannelsTap.get())
            artifact = TextResult(text=text)
            return render_for_llm(artifact), artifact

        ToolCallIdField.attach_all([tap_probe])
        ToolRunLogger.guard_all([tap_probe], lambda tool, call_id: stream)

        await tap_probe.ainvoke(
            {
                "name": "tap_probe",
                "args": {"text": "ping"},
                "id": "call-tap-1",
                "type": "tool_call",
            }
        )

        if seen != [stream]:
            raise AssertionError("seen == [stream]")
        if ToolChannelsTap.get() is not None:
            raise AssertionError("ToolChannelsTap.get() is None")
        if stream.note != "finished":
            raise AssertionError('stream.note == "finished"')
