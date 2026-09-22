"""Набор инструментов сервиса: функции через общий конвейер под запускателем без
ролей, профилей и контекста вызова; отказ инструментам про пользователя."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import pytest
from langchain_core.messages import ToolMessage
from pydantic import SecretStr

from boba.identity.context import Scope
from boba.runtime.plugins import ToolSet, ToolSetError
from boba.stand_core.fake_toolmod import FakeConfig, fake_echo
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import MarkdownResult
from boba.toolrun.injected import ToolConfigError
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller
from boba.toolrun.wrapping import ToolSchema

CFG = FakeConfig(token=SecretStr("t0ken"), limit=5)


def launcher(workdir: Path) -> ProcessToolCaller:
    values = {
        "provider": "process",
        "workdir": str(workdir),
        "timeout_sec": 60.0,
        "channel_limit_bytes": 1_000_000,
        "stderr_tail_bytes": 4096,
        "kill_grace_sec": 1.0,
    }

    return ProcessToolCaller("fake", ProcessLauncherConfig.model_validate(values))


def call(tool_, args: dict[str, object], call_id: str) -> ToolMessage:
    """Вызов как из графа: полный конверт ToolCall, ответ — ToolMessage."""
    envelope = {"name": tool_.name, "args": args, "id": call_id, "type": "tool_call"}

    async def go() -> ToolMessage:
        return await tool_.ainvoke(envelope)

    return asyncio.run(go())


@tool
async def needs_scope(text: str, scope: Annotated[Scope, Injected]) -> MarkdownResult:
    """Инструмент, которому нужна область вызова."""
    return MarkdownResult(text=f"{scope.id}: {text}")


class TestToolSet:
    def test_tool_runs_under_the_launcher_with_injected_config(
        self, tmp_path: Path
    ) -> None:
        tools = ToolSet(launcher(tmp_path), [CFG]).build([fake_echo])

        assert [tool_.name for tool_ in tools] == ["fake_echo"]
        schema = ToolSchema.of(tools[0])
        assert schema is not None
        assert "cfg" not in schema.model_fields

        reply = call(tools[0], {"text": "hi", "repeat": 2}, "call-1")

        assert "hi hi|t0ken" in str(reply.content)
        assert reply.artifact is not None
        assert reply.artifact.ok is True

    def test_body_failure_becomes_a_result(self, tmp_path: Path) -> None:
        tools = ToolSet(launcher(tmp_path), [CFG]).build([fake_echo])

        reply = call(tools[0], {"text": "crash", "repeat": 1}, "call-2")

        assert reply.artifact is not None
        assert reply.artifact.ok is False
        assert "crash" in str(reply.content)

    def test_module_singleton_is_left_untouched(self, tmp_path: Path) -> None:
        ToolSet(launcher(tmp_path), [CFG]).build([fake_echo])

        assert "cfg" in fake_echo.args_schema.model_fields

    def test_missing_injected_value_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ToolConfigError, match="FakeConfig"):
            ToolSet(launcher(tmp_path), []).build([fake_echo])

    def test_user_bound_tool_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ToolSetError, match="Scope of a call context"):
            ToolSet(launcher(tmp_path), [CFG]).build([needs_scope])
