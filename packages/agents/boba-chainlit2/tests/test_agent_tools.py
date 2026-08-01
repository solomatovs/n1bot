"""Тесты инструментов-прототипов: bash_local (shell) и visualize (chart)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from langchain_core.messages import ToolMessage
from pydantic import BaseModel

from boba.chainlit2.agent.tools import build_bash_local_tool, visualize
from boba.chainlit2.agent.tools.config import BashLocalConfig
from boba.chainlit2.rendering.tool_result import ChartResult, JsonResult


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Чистые юнит-тесты инструментов не требуют HTTP-контекста chainlit."""


def _tool_call(name: str, args: dict) -> dict:
    return {"args": args, "id": f"call-{name}", "name": name, "type": "tool_call"}


class TestBashLocal:
    def test_build_requires_workspace_root(self, tmp_path: Path) -> None:
        cfg = BashLocalConfig(workspace_root=tmp_path)
        tool = build_bash_local_tool(cfg)
        assert tool.name == "bash_local"
        schema = cast(type[BaseModel], tool.args_schema)
        assert set(schema.model_fields) == {"command", "stdin"}

    def test_echo(self, tmp_path: Path) -> None:
        cfg = BashLocalConfig(workspace_root=tmp_path)
        tool = build_bash_local_tool(cfg)
        msg: ToolMessage = tool.invoke(
            _tool_call("bash_local", {"command": "echo hello", "stdin": ""})
        )
        assert isinstance(msg.artifact, JsonResult)
        payload = msg.artifact.payload
        assert payload["exit_code"] == 0
        assert payload["stdout"] == "hello\n"
        assert payload["timed_out"] is False
        assert "hello" in msg.content

    def test_stdin_passed_to_command(self, tmp_path: Path) -> None:
        cfg = BashLocalConfig(workspace_root=tmp_path)
        tool = build_bash_local_tool(cfg)
        msg: ToolMessage = tool.invoke(
            _tool_call(
                "bash_local",
                {"command": "cat", "stdin": "line1\nline2\n"},
            )
        )
        assert msg.artifact.payload["stdout"] == "line1\nline2\n"

    def test_nonzero_exit_is_not_error(self, tmp_path: Path) -> None:
        cfg = BashLocalConfig(workspace_root=tmp_path)
        tool = build_bash_local_tool(cfg)
        msg: ToolMessage = tool.invoke(
            _tool_call("bash_local", {"command": "exit 3", "stdin": ""})
        )
        assert msg.artifact.payload["exit_code"] == 3
        assert msg.status == "success"

    def test_timeout_kills_process(self, tmp_path: Path) -> None:
        cfg = BashLocalConfig(workspace_root=tmp_path, timeout_sec=1)
        tool = build_bash_local_tool(cfg)
        msg: ToolMessage = tool.invoke(
            _tool_call("bash_local", {"command": "sleep 10", "stdin": ""})
        )
        assert msg.artifact.payload["timed_out"] is True
        assert msg.artifact.payload["exit_code"] == -9


class TestVisualize:
    def test_valid_spec_returns_chart(self) -> None:
        spec = '{"data":[{"type":"bar","x":[1,2],"y":[3,1]}],"layout":{"title":"T"}}'
        msg: ToolMessage = visualize.invoke(_tool_call("visualize", {"spec": spec}))
        assert isinstance(msg.artifact, ChartResult)
        assert msg.artifact.title == "T"
        layout_title = msg.artifact.spec["layout"]["title"]
        assert layout_title in ("T", {"text": "T"})
        assert msg.content == "[chart rendered: T]"

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(RuntimeError, match="JSON"):
            visualize.invoke(_tool_call("visualize", {"spec": "{not json"}))

    def test_non_object_spec_raises(self) -> None:
        with pytest.raises(RuntimeError, match="JSON-объектом"):
            visualize.invoke(_tool_call("visualize", {"spec": "[1,2,3]"}))

    def test_invalid_plotly_spec_raises(self) -> None:
        spec = '{"data": 42}'
        with pytest.raises(RuntimeError, match="невалидный Plotly"):
            visualize.invoke(_tool_call("visualize", {"spec": spec}))
