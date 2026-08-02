"""Tool bash: запуск shell-команды в песочнице.

Профиль берётся из конфига инструмента: LLM не выбирает окружение и не
знает, что команда исполняется в контейнере.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from typing import Annotated, Any

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.chainlit2.agent.tools.sandbox.config import BashSandboxConfig
from boba.chainlit2.rendering.render import pack_result
from boba.chainlit2.rendering.tool_result import JsonResult, ToolResult
from boba.chainlit2.sandbox import SandboxOutcome, SandboxRunner

__all__ = ["build_bash_tool", "has_bwrap"]

_MAX_COMMAND_LEN = 16_384
_MAX_STDIN_LEN = 1 * 1024 * 1024


def has_bwrap() -> bool:
    return shutil.which("bwrap") is not None


def build_bash_tool(
    cfg: BashSandboxConfig,
    path_vars: Callable[[], Mapping[str, str]],
) -> BaseTool:
    runner = SandboxRunner(cfg.sandbox, path_vars)

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
        """Выполнить shell-команду и вернуть её вывод.

        Доступ к файловой системе и сети ограничен окружением запуска.
        """
        outcome = runner.run(command, profile_name=cfg.profile, stdin=stdin)
        return pack_result(
            JsonResult(ok=outcome.succeeded, payload=_result_to_payload(outcome))
        )

    return bash


def _result_to_payload(outcome: SandboxOutcome) -> dict[str, Any]:
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
