"""Tool bash: команда пользователя одним узлом графа стадий.

Фасад строит вырожденный WorkflowSpec из узла bash, отдаёт его порту запуска и
собирает ответ из головы журнала канала данных и процессных фактов стадии.
Полный поток команды живёт в журнале стадии; в ленту едет только голова.
"""

from __future__ import annotations

from typing import Annotated, ClassVar

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

from boba.tool.shell.protocol import BashArgs, BashStage
from boba.toolkit.channels import Channel
from boba.toolkit.launcher import (
    ChannelHead,
    ErrorKind,
    LauncherError,
    LauncherFactory,
    StageFailure,
    StageRun,
    ToolLauncher,
)
from boba.toolkit.result import JsonResult, ToolResult, pack_result
from boba.toolkit.workflow import WorkflowError, WorkflowOutcome

__all__ = ["BashRun", "build_bash_tool"]


class BashAnswer(BaseModel):
    """Ответ модели на успешный ход стадии."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    stdout: str
    truncated_stdout: bool
    duration_ms: int
    timed_out: bool
    diagnostic: str


class BashFailureAnswer(BaseModel):
    """Ответ модели на сорвавшуюся стадию: код возврата объясняет текст раннера."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stdout: str
    truncated_stdout: bool
    error_kind: str
    message: str


class BashRun:
    """Одиночный вызов bash: граф из одного узла и ответ из его каналов."""

    STAGE: ClassVar[str] = BashStage.NAME

    MAX_STDIN: ClassVar[int] = 1024 * 1024

    def __init__(self, launcher: ToolLauncher, max_output_bytes: int) -> None:
        self._run = StageRun(launcher)
        self._max_output_bytes = max_output_bytes

    def run(self, command: str, stdin: str) -> ToolResult:
        literal: str | None = None
        if stdin:
            literal = stdin

        args = BashArgs(command=command)

        try:
            outcome = self._run.call(
                self.STAGE,
                args.model_dump(mode="json"),
                stdin=literal,
            )
        except (LauncherError, WorkflowError) as exc:
            head = self._head(StageFailure.outcome_of(exc))
            return JsonResult(ok=False, payload=self._failed(head, exc))

        head = self._head(outcome)

        stage = outcome.outcome_of(self.STAGE)
        answer = BashAnswer(
            exit_code=stage.exit_code,
            stdout=head.text,
            truncated_stdout=head.truncated,
            duration_ms=stage.duration_ms,
            timed_out=stage.timed_out,
            diagnostic=stage.diagnostic,
        )

        return JsonResult(ok=True, payload=answer.model_dump(mode="json"))

    def _head(self, outcome: WorkflowOutcome) -> ChannelHead:
        """Голова журнала продукта: stdout команды течёт в tool_payload."""
        return self._run.head(
            outcome,
            self.STAGE,
            Channel.TOOL_PAYLOAD,
            self._max_output_bytes,
        )

    @staticmethod
    def _failed(head: ChannelHead, error: Exception) -> dict[str, object]:
        """Голова вывода остаётся в ответе: команда успела что-то напечатать."""
        answer = BashFailureAnswer(
            stdout=head.text,
            truncated_stdout=head.truncated,
            error_kind=ErrorKind.of(error),
            message=str(error),
        )

        return answer.model_dump(mode="json")


def build_bash_tool(launchers: LauncherFactory, max_output_bytes: int) -> BaseTool:
    """max_output_bytes — голова канала данных в ответе; берётся из профиля узла."""
    runner = BashRun(launchers(BashStage.NAME), max_output_bytes)

    @tool(response_format="content_and_artifact")
    def bash(
        command: Annotated[
            str,
            Field(
                min_length=1,
                max_length=BashArgs.MAX_COMMAND,
                description="Shell-команда (передаётся в `bash -c`).",
            ),
        ],
        stdin: Annotated[
            str,
            Field(
                max_length=BashRun.MAX_STDIN,
                description="Stdin для команды (UTF-8). Пустая строка = нет stdin.",
            ),
        ] = "",
    ) -> tuple[str, ToolResult]:
        """Выполнить shell-команду и вернуть вывод; доступ к ФС и сети ограничен."""
        return pack_result(runner.run(command, stdin))

    return bash
