"""Конвейер из чата: каталог узлов, запуск цепочки, отказы стыковки.

Интеграция на реальных процессах: узлы исполняются через ToolInvoker с
обёрткой запуска, данные между ними едут splice'ом через ядро.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from langchain_core.tools import StructuredTool
from pydantic import SecretStr

from boba.stand.fake_toolmod import FakeConfig, fake_echo, fake_relay, fake_stream
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import PayloadTool
from boba.toolkit.result import ErrorResult, TextResult
from boba.toolkit.wrap import ToolProcessWrap
from boba.toolrun.intent import ToolIntentField
from boba.toolrun.invoke import ToolInvoker
from boba.toolrun.pipeline import PipelineService
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller

CFG = FakeConfig(token=SecretStr("t0ken"), limit=5)


def _invoker(workdir: Path) -> ToolInvoker:
    """Инвокер трёх инструментов стенда поверх субпроцессного лончера."""
    launcher = ProcessToolCaller(
        "pipe",
        ProcessLauncherConfig.model_validate(
            {
                "provider": "process",
                "workdir": str(workdir),
                "shell": "/bin/bash",
                "timeout_sec": 60.0,
                "channel_limit_bytes": 8_000_000,
                "stderr_tail_bytes": 4096,
                "kill_grace_sec": 0.5,
            }
        ),
    )

    copies: list[PayloadTool] = []
    for tool in ToolMain.toolset(fake_stream, fake_relay, fake_echo):
        assert isinstance(tool, PayloadTool)
        copies.append(tool.model_copy())

    ToolProcessWrap.guard_all(copies, launcher)

    bridged: dict[str, StructuredTool] = {}
    for copy in copies:
        bridged[copy.name] = StructuredTool(
            name=copy.name,
            description=copy.description,
            args_schema=copy.args_schema,
            func=copy.func,
            coroutine=copy.coroutine,
            response_format=PayloadTool.RESPONSE_FORMAT,
        )

    ToolIntentField.attach_all(list(bridged.values()))

    return ToolInvoker(bridged)


def _plan(*nodes: tuple[str, dict[str, object]]) -> str:
    listed = [{"tool": name, "args": args} for name, args in nodes]
    return json.dumps({"nodes": listed})


class TestCatalog:
    def test_streaming_tools_only(self, tmp_path: Path) -> None:
        invoker = _invoker(tmp_path)

        result = PipelineService().catalog(invoker)

        assert isinstance(result, TextResult)
        assert "fake_stream" in result.text
        assert "fake_relay" in result.text
        assert "fake_echo" not in result.text
        assert "in=raw out=raw" in result.text
        assert "chunk|done" in result.text


class TestRun:
    def test_framed_chain_moves_data_through_the_kernel(
        self, tmp_path: Path
    ) -> None:
        """Цепочка fake_stream -> fake_stream: done-кадр первого узла едет
        сквозь ядро во второй, конверты обоих узлов — в отчёте."""
        invoker = _invoker(tmp_path)

        plan = _plan(
            ("fake_stream", {"prefix": "a:", "cfg": CFG.revealed()}),
            ("fake_stream", {"prefix": "b:", "cfg": CFG.revealed()}),
        )

        result = asyncio.run(PipelineService().run(invoker, plan))

        assert isinstance(result, TextResult), result
        assert "streamed 0" in result.text
        assert "streamed 1" in result.text
        assert "bytes moved" in result.text

    def test_mixed_modes_are_refused_before_start(self, tmp_path: Path) -> None:
        invoker = _invoker(tmp_path)

        plan = _plan(
            ("fake_stream", {"prefix": "a:", "cfg": CFG.revealed()}),
            ("fake_relay", {"cfg": CFG.revealed()}),
        )

        result = asyncio.run(PipelineService().run(invoker, plan))

        assert isinstance(result, ErrorResult)
        assert "do not mix" in result.message

    def test_unknown_node_is_named(self, tmp_path: Path) -> None:
        invoker = _invoker(tmp_path)

        plan = _plan(("no_such", {}), ("fake_relay", {"cfg": CFG.revealed()}))

        result = asyncio.run(PipelineService().run(invoker, plan))

        assert isinstance(result, ErrorResult)
        assert "unknown pipeline node 'no_such'" in result.message

    def test_plain_tool_is_not_a_node(self, tmp_path: Path) -> None:
        invoker = _invoker(tmp_path)

        plan = _plan(
            ("fake_echo", {"text": "x", "repeat": 1, "cfg": CFG.revealed()}),
            ("fake_relay", {"cfg": CFG.revealed()}),
        )

        result = asyncio.run(PipelineService().run(invoker, plan))

        assert isinstance(result, ErrorResult)
        assert "declares no stream ports" in result.message

    def test_broken_plan_json_is_reported(self, tmp_path: Path) -> None:
        invoker = _invoker(tmp_path)

        result = asyncio.run(PipelineService().run(invoker, "not a json"))

        assert isinstance(result, ErrorResult)
        assert "plan is invalid" in result.message
