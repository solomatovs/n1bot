"""Tool bash: запуск shell-команды в песочнице."""

from __future__ import annotations

from enum import IntEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.calls import ScriptCall, ToolCallViews
from boba.toolkit.facade import PayloadTool, tool
from boba.toolkit.launcher import ClippedText, LauncherFactory, LaunchOutcome
from boba.toolkit.result import ShellResult, ToolResult, pack_result

__all__ = ["BashToolConfig", "build_bash_tool"]


class BashInputLimit(IntEnum):
    """Потолки входа инструмента: длина команды и длина stdin."""

    COMMAND_CHARS = 16_384
    STDIN_CHARS = 1024 * 1024


class BashToolConfig(BaseModel):
    """Потолок вывода команды: сколько байт stdout/stderr доходит до LLM."""

    model_config = ConfigDict(extra="ignore")

    max_output_bytes: int = Field(
        gt=0,
        description="Аварийный потолок объёма каждого потока вывода (байт).",
    )


class BashOutput:
    """Сборка результата команды: потоки урезаются до потолка конфига."""

    @staticmethod
    def of(outcome: LaunchOutcome, limits: BashToolConfig) -> ShellResult:
        result = outcome.result
        budget = limits.max_output_bytes

        stdout = ClippedText.of(result.stdout, budget)
        stderr = ClippedText.of(result.stderr, budget)

        return ShellResult(
            ok=outcome.succeeded,
            exit_code=result.exit_code,
            stdout=stdout.text,
            stdout_bytes=stdout.total_bytes,
            stdout_truncated=stdout.truncated,
            stderr=stderr.text,
            stderr_bytes=stderr.total_bytes,
            stderr_truncated=stderr.truncated,
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            diagnostic=outcome.diagnostic,
        )


def build_bash_tool(cfg: BashToolConfig, launchers: LauncherFactory) -> PayloadTool:
    caller = launchers("bash")
    ToolCallViews.register("bash", ScriptCall(arg="command", lang="bash"))

    @tool
    def bash(
        command: Annotated[
            str,
            Field(
                min_length=1,
                max_length=BashInputLimit.COMMAND_CHARS,
                description="Shell-команда (передаётся в `bash -c`).",
            ),
        ],
        stdin: Annotated[
            str,
            Field(
                max_length=BashInputLimit.STDIN_CHARS,
                description="Stdin для команды (UTF-8). Пустая строка = нет stdin.",
            ),
        ] = "",
    ) -> tuple[str, ToolResult]:
        """Выполнить shell-команду и вернуть вывод; доступ к ФС и сети ограничен.

        Объём вывода ограничивайте самой командой (head, tail, grep, wc): всё,
        что вышло за аварийный потолок, отброшено, а ответ помечен truncated.
        """
        outcome = caller.call_text(command, stdin=stdin)

        return pack_result(BashOutput.of(outcome, cfg))

    return bash
