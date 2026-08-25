"""Конвейер обёрток приложения: ToolCall-конверт -> обёртки -> тело функции.

Сборка повторяет загрузчик поверх тела: InjectedConfig -> ToolCallIdField
-> ToolRunLogger; вызов — ainvoke полным ToolCall, как зовёт ToolNode агента.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, ClassVar

import pytest
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.tools import InjectedToolArg, tool
from pydantic import BaseModel, Field, SecretStr

from boba.chainlit.agent.toolrun.call_id import ToolCallIdField
from boba.chainlit.agent.toolrun.injected import InjectedConfig
from boba.chainlit.agent.toolrun.intent import ToolIntentField
from boba.chainlit.agent.toolrun.run_log import CallStream, NoCallScope, ToolRunLogger
from boba.chainlit.agent.toolrun.wrapping import ToolAsyncBody
from boba.chainlit.rendering.tool import MarkdownRendering, ToolResultView
from boba.toolkit.calls import ToolIntent
from boba.toolkit.channels import JournalChannel
from boba.toolkit.result import (
    TextResult,
    ToolArtifact,
    ToolResult,
    render_for_llm,
)
from boba.toolkit.stream import ToolChannelsTap


class PipeConfig(BaseModel):
    SECTION: ClassVar[str] = "tool.pipe"

    token: SecretStr


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
        artifact = TextResult(text=f"{text}|{cfg.token.get_secret_value()}")
        return render_for_llm(artifact), artifact

    InjectedConfig.bind_all(
        [pipe_echo],
        lambda name, annotation: PipeConfig(token=SecretStr("p1p3")),
    )
    ToolCallIdField.attach_all([pipe_echo])
    ToolIntentField.attach_all([pipe_echo])
    ToolRunLogger.guard_all([pipe_echo], lambda tool, call_id: None, NoCallScope.enter)

    return pipe_echo


def call_envelope(text: str) -> dict[str, Any]:
    return {
        "name": "pipe_echo",
        "args": {"text": text, ToolIntent.NAME: "показываю эхо"},
        "id": "call-pipe-1",
        "type": "tool_call",
    }


class TestPipeline:
    def test_llm_schema_hides_injected_and_call_id(self) -> None:
        pipe_echo = build_pipeline()

        schema = pipe_echo.tool_call_schema
        if list(schema.model_fields) != ["text", ToolIntent.NAME]:
            raise AssertionError(list(schema.model_fields))

    def test_tool_call_invocation_reaches_the_body(self) -> None:
        pipe_echo = build_pipeline()

        message = asyncio.run(pipe_echo.ainvoke(call_envelope("hi")))

        if "hi|p1p3" not in str(message.content):
            raise AssertionError('"hi|p1p3" in str(message.content)')

    def test_intent_is_offered_but_optional(self) -> None:
        """Подпись видна модели, но вызов без неё проходит: шаг зовётся именем тула."""
        pipe_echo = build_pipeline()

        schema = pipe_echo.tool_call_schema
        info = schema.model_fields[ToolIntent.NAME]
        if info.is_required():
            raise AssertionError("подпись вызова не должна ронять вызов")

        envelope = call_envelope("hi")
        envelope["args"] = ToolIntent.without(envelope["args"])
        message = asyncio.run(pipe_echo.ainvoke(envelope))
        if "hi|p1p3" not in str(message.content):
            raise AssertionError(str(message.content))

    def test_intent_does_not_reach_the_body(self) -> None:
        """Подпись вызова снимается обвязкой: тело о поле не знает."""
        pipe_echo = build_pipeline()

        envelope = call_envelope("hi")

        message = asyncio.run(pipe_echo.ainvoke(envelope))

        if "hi|p1p3" not in str(message.content):
            raise AssertionError('"hi|p1p3" in str(message.content)')


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

    def test_retired_kind_is_not_revived(self) -> None:
        """Вариант pg_copy_text удалён без совместимости: ревив отдаёт None."""
        legacy = {"kind": "pg_copy_text", "ok": True, "text": "n\n1\n"}

        if ToolArtifact.revive(legacy) is not None:
            raise AssertionError("ToolArtifact.revive(legacy) is None")


class _FakeStream(CallStream):
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
        ToolRunLogger.guard_all(
            [tap_probe], lambda tool, call_id: stream, NoCallScope.enter
        )

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


class _LoopRecorder(AsyncCallbackHandler):
    """Запоминает, в каком event loop langchain позвал on_tool_start."""

    def __init__(self) -> None:
        self.loops: list[int] = []

    async def on_tool_start(self, *args: Any, **kwargs: Any) -> None:
        self.loops.append(id(asyncio.get_running_loop()))


class TestAsyncBody:
    """Sync-инструмент получает корутину: колбэки трасера остаются в loop хода.

    Без неё StructuredTool.ainvoke уводит весь вызов в поток, где langchain
    запускает async-колбэки через собственный Runner — в чужом loop, и шаги
    ленты не доходят до пула postgres.
    """

    @staticmethod
    def _sync_tool() -> Any:
        @tool
        def sync_echo(text: str) -> str:
            """Возвращает текст."""
            return text

        return sync_echo

    @pytest.mark.anyio
    async def test_callbacks_run_in_the_caller_loop(self) -> None:
        sync_echo = self._sync_tool()
        ToolAsyncBody.ensure_all([sync_echo])

        recorder = _LoopRecorder()
        envelope = {
            "name": "sync_echo",
            "args": {"text": "hi"},
            "id": "call-loop-1",
            "type": "tool_call",
        }
        message = await sync_echo.ainvoke(envelope, config={"callbacks": [recorder]})

        if "hi" not in str(message.content):
            raise AssertionError(f"тело исполнилось: {message.content!r}")

        if recorder.loops != [id(asyncio.get_running_loop())]:
            raise AssertionError(f"колбэк шёл в чужом loop: {recorder.loops}")

    @pytest.mark.anyio
    async def test_without_coroutine_callbacks_leave_the_loop(self) -> None:
        """Контроль: сам баг воспроизводится без корутины."""
        sync_echo = self._sync_tool()

        recorder = _LoopRecorder()
        envelope = {
            "name": "sync_echo",
            "args": {"text": "hi"},
            "id": "call-loop-2",
            "type": "tool_call",
        }
        await sync_echo.ainvoke(envelope, config={"callbacks": [recorder]})

        if recorder.loops == [id(asyncio.get_running_loop())]:
            raise AssertionError("без корутины колбэк ушёл бы в чужой loop")
