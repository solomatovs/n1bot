"""Tool bash: запуск shell-команды в песочнице."""

from __future__ import annotations

from typing import Annotated, Any

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.toolkit.launcher import LauncherFactory, LaunchOutcome
from boba.toolkit.result import JsonResult, ToolResult, pack_result

__all__ = ["build_bash_tool"]

_MAX_COMMAND_LEN = 16_384
_MAX_STDIN_LEN = 1 * 1024 * 1024


def build_bash_tool(launchers: LauncherFactory) -> BaseTool:
    caller = launchers("bash")

    @tool(response_format="content_and_artifact")
    def bash(
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
                description="Stdin для команды (UTF-8). Пустая строка = нет stdin.",
            ),
        ] = "",
    ) -> tuple[str, ToolResult]:
        """Выполнить shell-команду и вернуть вывод; доступ к ФС и сети ограничен."""
        outcome = caller.call_text(command, stdin=stdin)
        return pack_result(
            JsonResult(ok=outcome.succeeded, payload=_result_to_payload(outcome))
        )

    return bash


def _result_to_payload(outcome: LaunchOutcome) -> dict[str, Any]:
    result = outcome.result
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "truncated_stdout": result.truncated_stdout,
        "truncated_stderr": result.truncated_stderr,
        "timed_out": result.timed_out,
        "diagnostic": outcome.diagnostic,
    }
