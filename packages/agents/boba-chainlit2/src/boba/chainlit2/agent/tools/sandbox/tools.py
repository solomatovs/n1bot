"""Tool bash: запуск команды в bubblewrap-песочнице.

command уходит единичным argv-элементом в bash -c — без shell-
интерполяции на стороне Python; пути, env и cwd задаёт только конфиг.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.chainlit2.agent.tools.process.runner import RunResult, run_subprocess
from boba.chainlit2.agent.tools.sandbox.argv import build_bwrap_argv
from boba.chainlit2.agent.tools.sandbox.config import BashSandboxConfig
from boba.chainlit2.rendering.render import pack_result
from boba.chainlit2.rendering.tool_result import JsonResult, ToolResult

__all__ = ["build_bash_tool", "has_bwrap"]

_MAX_COMMAND_LEN = 16_384
_MAX_STDIN_LEN = 1 * 1024 * 1024


def has_bwrap() -> bool:
    return shutil.which("bwrap") is not None


def build_bash_tool(
    cfg: BashSandboxConfig,
    path_vars: Callable[[], Mapping[str, str]],
) -> BaseTool:

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
                description=(
                    "Stdin для команды (UTF-8). Пустая строка = нет stdin."
                ),
            ),
        ] = "",
        profile: Annotated[
            str,
            Field(
                description=(
                    "Имя профиля песочницы. Пустая строка = default из конфига."
                ),
            ),
        ] = "",
    ) -> tuple[str, ToolResult]:
        """Выполнить shell-команду в изолированной bubblewrap-песочнице.

        Отдельные user/PID/IPC/UTS namespace'ы, сеть и доступ к ФС — по
        выбранному профилю.
        """
        profile_name = profile or cfg.default_profile
        profile_dto = cfg.profiles.get(profile_name)
        if profile_dto is None:
            return pack_result(
                JsonResult(
                    ok=False,
                    payload=_unknown_profile_payload(
                        profile_name, sorted(cfg.profiles)
                    ),
                )
            )

        rendered = profile_dto.render(path_vars())
        for spec in rendered.rw_binds:
            Path(spec.host).mkdir(parents=True, exist_ok=True)
        argv = build_bwrap_argv(rendered, command, env=rendered.env_set)
        result = run_subprocess(
            argv,
            stdin_data=stdin.encode("utf-8"),
            timeout_sec=rendered.timeout_sec,
            max_output_bytes=rendered.max_output_bytes,
            cwd="/",
            env=os.environ,
        )
        return pack_result(
            JsonResult(
                ok=_succeeded(result),
                payload=_result_to_payload(result, profile_name),
            )
        )

    return bash


def _succeeded(result: RunResult) -> bool:
    return result.exit_code == 0 and not result.timed_out


def _unknown_profile_payload(name: str, available: list[str]) -> dict[str, Any]:
    return {
        "exit_code": -1,
        "stdout": "",
        "stderr": f"unknown sandbox profile: {name!r}; available: {available}",
        "duration_ms": 0,
        "truncated_stdout": False,
        "truncated_stderr": False,
        "timed_out": False,
        "profile": name,
        "error_kind": "unknown_profile",
    }


def _result_to_payload(result: RunResult, profile: str) -> dict[str, Any]:
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "truncated_stdout": result.truncated_stdout,
        "truncated_stderr": result.truncated_stderr,
        "timed_out": result.timed_out,
        "profile": profile,
    }
