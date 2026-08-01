"""Tool bash_local: shell БЕЗ изоляции — доступ к ФС и сети как у агента.

Для недоверенного кода используйте bash (bwrap-песочница).
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.chainlit2.agent.tools.config import BashLocalConfig
from boba.chainlit2.agent.tools.profile_local import resolve_local_env
from boba.chainlit2.agent.tools.runner import RunResult, run_subprocess
from boba.chainlit2.rendering.render import pack_result
from boba.chainlit2.rendering.tool_result import JsonResult, ToolResult

__all__ = ["build_bash_local_tool"]

_MAX_COMMAND_LEN = 16_384
_MAX_STDIN_LEN = 1 * 1024 * 1024
_BASH_BIN = "/bin/bash"


def build_bash_local_tool(cfg: BashLocalConfig) -> BaseTool:

    @tool(response_format="content_and_artifact")
    def bash_local(
        command: Annotated[
            str,
            Field(
                min_length=1,
                max_length=_MAX_COMMAND_LEN,
                description="Shell-команда (передаётся в `bash -c`).",
            ),
        ],
        stdin: Annotated[
            str,
            Field(
                max_length=_MAX_STDIN_LEN,
                description=(
                    "Stdin для команды (UTF-8). Пустая строка = нет stdin."
                ),
            ),
        ] = "",
    ) -> tuple[str, ToolResult]:
        """Выполнить shell-команду через bash -c без изоляции."""
        argv = [_BASH_BIN, "-c", command]
        cwd = cfg.cwd or str(cfg.workspace_root)
        env = resolve_local_env(cfg.env_passthrough, cfg.env_set, os.environ)
        result = run_subprocess(
            argv,
            stdin_data=stdin.encode("utf-8"),
            timeout_sec=cfg.timeout_sec,
            max_output_bytes=cfg.max_output_bytes,
            cwd=cwd,
            env=env,
        )
        return pack_result(
            JsonResult(ok=_succeeded(result), payload=_result_to_payload(result))
        )

    return bash_local


def _succeeded(result: RunResult) -> bool:
    return result.exit_code == 0 and not result.timed_out


def _result_to_payload(result: RunResult) -> dict[str, Any]:
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "truncated_stdout": result.truncated_stdout,
        "truncated_stderr": result.truncated_stderr,
        "timed_out": result.timed_out,
    }
