"""Конвейер обёрток вокруг тела инструмента: вызов-узел -> обёртки -> тело.

Сборка повторяет загрузчик поверх тела плагина: обёртка запуска по
объявлению манифеста и секция плагина (InjectedConfig) -> CallFields ->
ToolRunLogger; вызов — HostedTool.call узлом DagNode, как зовёт
исполнитель. Проверяется схема вызова для модели, путь аргументов до тела и
то, что результат тела оживает из сериализованного вида истории.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from boba.identity.context import CallContexts
from boba.identity.run import Runs
from boba.stand.refs import StandRefs
from boba.stand_core.fake_toolmod import fake_echo
from boba.toolkit.calls import ToolIntent
from boba.toolkit.chain import CallAmbient
from boba.toolkit.dag import DagNode
from boba.toolkit.result import MarkdownResult
from boba.toolrun.call_id import CallFields
from boba.toolrun.hosted import HostedTool, PluginSection, ToolSchema
from boba.toolrun.injected import InjectedConfig, StaticSection
from boba.toolrun.run_log import ToolRunLogger
from boba.toolrun.streams import CallJournals

PLUGINS = ("boba.stand_core.fake_toolmod:PLUGIN",)
SECTION = PluginSection(values={"token": "p1p3", "limit": 5})


def build_pipeline(workdir: Path) -> HostedTool:
    """Свежий инструмент плагина, обёрнутый как в загрузчике."""
    contexts = CallContexts()
    stand = StandRefs(contexts).tool_stand((InjectedConfig(StaticSection()),))
    launcher = stand.process_launcher("pipe", workdir, 60.0, PLUGINS)
    (bridged,) = stand.launch([fake_echo], launcher, SECTION)

    CallFields().attach_all([bridged])
    ToolRunLogger(
        CallJournals(None, Runs(contexts)), contexts, CallAmbient()
    ).guard_all([bridged])

    return bridged


def call_envelope(text: str) -> DagNode:
    return DagNode(
        key="call-pipe-1",
        tool="fake_echo",
        args={"text": text, "repeat": 1, ToolIntent.NAME: "показываю эхо"},
    )


class TestPipeline:
    def test_llm_schema_hides_injected_and_call_id(self, tmp_path: Path) -> None:
        pipe_echo = build_pipeline(tmp_path)

        schema = ToolSchema().card_of(pipe_echo).parameters
        if list(schema["properties"]) != ["text", "repeat", ToolIntent.NAME]:
            raise AssertionError(list(schema["properties"]))

    def test_tool_call_invocation_reaches_the_body(self, tmp_path: Path) -> None:
        pipe_echo = build_pipeline(tmp_path)

        message = asyncio.run(pipe_echo.call(call_envelope("hi")))

        if "hi|p1p3" not in str(message.content):
            raise AssertionError('"hi|p1p3" in str(message.content)')

    def test_intent_is_offered_but_optional(self, tmp_path: Path) -> None:
        """Подпись видна модели, но вызов без неё проходит: шаг зовётся именем тула."""
        pipe_echo = build_pipeline(tmp_path)

        schema = ToolSchema().card_of(pipe_echo).parameters
        if ToolIntent.NAME not in schema["properties"]:
            raise AssertionError("подпись вызова видна модели")
        if ToolIntent.NAME in schema["required"]:
            raise AssertionError("подпись вызова не должна ронять вызов")

        envelope = DagNode(
            key="call-pipe-1", tool="fake_echo", args={"text": "hi", "repeat": 1}
        )
        message = asyncio.run(pipe_echo.call(envelope))
        if "hi|p1p3" not in str(message.content):
            raise AssertionError(str(message.content))

    def test_intent_does_not_reach_the_body(self, tmp_path: Path) -> None:
        """Подпись вызова снимается обвязкой: тело о поле не знает."""
        pipe_echo = build_pipeline(tmp_path)

        envelope = call_envelope("hi")

        message = asyncio.run(pipe_echo.call(envelope))

        if "hi|p1p3" not in str(message.content):
            raise AssertionError('"hi|p1p3" in str(message.content)')


class TestArtifactRendering:
    """2c: артефакт нового пути — результат семейства, рендерится существующим
    механизмом."""

    def test_artifact_from_pipeline_renders(self, tmp_path: Path) -> None:
        pipe_echo = build_pipeline(tmp_path)

        message = asyncio.run(pipe_echo.call(call_envelope("hi")))

        artifact = message.artifact
        if not isinstance(artifact, MarkdownResult):
            raise AssertionError(f"pipeline artifact is markdown, got {artifact!r}")

        if "hi|p1p3" not in artifact.text:
            raise AssertionError(f"pipeline artifact text: {artifact.text!r}")
