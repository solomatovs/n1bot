"""Конвейер обёрток вокруг тела инструмента: вызов-узел -> обёртки -> тело.

Сборка повторяет загрузчик поверх тела: InjectedConfig -> CallFields
-> ToolRunLogger; вызов — HostedTool.call узлом DagNode, как зовёт
исполнитель. Проверяется схема вызова для модели, путь аргументов до тела и
то, что результат тела оживает из сериализованного вида истории.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, ClassVar

from pydantic import BaseModel, Field, SecretStr

from boba.identity.context import CallContexts
from boba.identity.run import Runs
from boba.toolkit.calls import ToolIntent
from boba.toolkit.chain import CallAmbient
from boba.toolkit.dag import DagNode
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import MarkdownResult
from boba.toolrun.call_id import CallFields
from boba.toolrun.hosted import HostedTool, ToolHosting, ToolSchema
from boba.toolrun.injected import InjectedConfig, StaticConfig
from boba.toolrun.run_log import ToolRunLogger
from boba.toolrun.streams import CallJournals


class PipeConfig(BaseModel):
    SECTION: ClassVar[str] = "tool.pipe"

    token: SecretStr


def build_pipeline() -> HostedTool:
    """Свежий инструмент, обёрнутый как в загрузчике."""

    @tool
    async def pipe_echo(
        text: Annotated[str, Field(min_length=1, description="Что вернуть")],
        *,
        cfg: Annotated[PipeConfig, Injected],
    ) -> MarkdownResult:
        """Возвращает текст с секретом конфига."""
        return MarkdownResult(text=f"{text}|{cfg.token.get_secret_value()}")

    bridged = ToolHosting().hosted(pipe_echo)
    InjectedConfig(
        lambda name, annotation: PipeConfig(token=SecretStr("p1p3")), StaticConfig()
    ).bind_all([bridged])
    CallFields().attach_all([bridged])
    contexts = CallContexts()
    ToolRunLogger(
        CallJournals(None, Runs(contexts)), contexts, CallAmbient()
    ).guard_all([bridged])

    return bridged


def call_envelope(text: str) -> DagNode:
    return DagNode(
        key="call-pipe-1",
        tool="pipe_echo",
        args={"text": text, ToolIntent.NAME: "показываю эхо"},
    )


class TestPipeline:
    def test_llm_schema_hides_injected_and_call_id(self) -> None:
        pipe_echo = build_pipeline()

        schema = ToolSchema().card_of(pipe_echo).parameters
        if list(schema["properties"]) != ["text", ToolIntent.NAME]:
            raise AssertionError(list(schema["properties"]))

    def test_tool_call_invocation_reaches_the_body(self) -> None:
        pipe_echo = build_pipeline()

        message = asyncio.run(pipe_echo.call(call_envelope("hi")))

        if "hi|p1p3" not in str(message.content):
            raise AssertionError('"hi|p1p3" in str(message.content)')

    def test_intent_is_offered_but_optional(self) -> None:
        """Подпись видна модели, но вызов без неё проходит: шаг зовётся именем тула."""
        pipe_echo = build_pipeline()

        schema = ToolSchema().card_of(pipe_echo).parameters
        if ToolIntent.NAME not in schema["properties"]:
            raise AssertionError("подпись вызова видна модели")
        if ToolIntent.NAME in schema["required"]:
            raise AssertionError("подпись вызова не должна ронять вызов")

        envelope = DagNode(key="call-pipe-1", tool="pipe_echo", args={"text": "hi"})
        message = asyncio.run(pipe_echo.call(envelope))
        if "hi|p1p3" not in str(message.content):
            raise AssertionError(str(message.content))

    def test_intent_does_not_reach_the_body(self) -> None:
        """Подпись вызова снимается обвязкой: тело о поле не знает."""
        pipe_echo = build_pipeline()

        envelope = call_envelope("hi")

        message = asyncio.run(pipe_echo.call(envelope))

        if "hi|p1p3" not in str(message.content):
            raise AssertionError('"hi|p1p3" in str(message.content)')


class TestArtifactRendering:
    """2c: артефакт нового пути — результат семейства, рендерится существующим
    механизмом."""

    def test_artifact_from_pipeline_renders(self) -> None:
        pipe_echo = build_pipeline()

        message = asyncio.run(pipe_echo.call(call_envelope("hi")))

        artifact = message.artifact
        if not isinstance(artifact, MarkdownResult):
            raise AssertionError(f"pipeline artifact is markdown, got {artifact!r}")

        if "hi|p1p3" not in artifact.text:
            raise AssertionError(f"pipeline artifact text: {artifact.text!r}")
