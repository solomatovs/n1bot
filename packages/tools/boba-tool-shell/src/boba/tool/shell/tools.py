"""Tool bash: команда пользователя одним узлом графа стадий.

Фасад строит вырожденный WorkflowSpec из узла bash, отдаёт его порту запуска и
собирает ответ из головы канала данных и процессных фактов стадии. Полный
поток команды живёт в журнале стадии; в ленту едет только голова.
"""

from __future__ import annotations

import codecs
from typing import Annotated, ClassVar

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

from boba.tool.shell.protocol import BashArgs, BashStage
from boba.toolkit.channels import ByteText, ChannelSink
from boba.toolkit.launcher import (
    ErrorKind,
    LauncherError,
    LauncherFactory,
    StageRun,
    ToolLauncher,
)
from boba.toolkit.result import JsonResult, ToolResult, pack_result
from boba.toolkit.workflow import WorkflowError

__all__ = ["BashRun", "StdoutHead", "build_bash_tool"]


class StdoutHead(ChannelSink):
    """Голова байтового потока в текст: хвост за потолком отмечается, но не хранится."""

    def __init__(self, max_bytes: int) -> None:
        if max_bytes <= 0:
            msg = f"max_bytes must be positive, got {max_bytes}"
            raise ValueError(msg)

        self._max_bytes = max_bytes
        self._decoder = codecs.getincrementaldecoder(ByteText.ENCODING)(
            ByteText.ERRORS
        )
        self._parts: list[str] = []
        self._taken = 0
        self._truncated = False

    @property
    def truncated(self) -> bool:
        return self._truncated

    def feed(self, data: bytes) -> None:
        room = self._max_bytes - self._taken

        if room <= 0:
            self._truncated = True
            return

        head = data[:room]

        if len(head) < len(data):
            self._truncated = True

        self._taken += len(head)
        self._parts.append(self._decoder.decode(head))

    def close(self) -> None:
        self._parts.append(self._decoder.decode(b"", final=True))

    def text(self) -> str:
        return "".join(self._parts)


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
        head = StdoutHead(self._max_output_bytes)

        literal: str | None = None
        if stdin:
            literal = stdin

        args = BashArgs(command=command)

        try:
            outcome = self._run.call(
                self.STAGE,
                args.model_dump(mode="json"),
                sink=head,
                stdin=literal,
            )
        except (LauncherError, WorkflowError) as exc:
            head.close()
            return JsonResult(ok=False, payload=self._failed(head, exc))

        head.close()

        stage = outcome.outcome_of(self.STAGE)
        answer = BashAnswer(
            exit_code=stage.exit_code,
            stdout=head.text(),
            truncated_stdout=head.truncated,
            duration_ms=stage.duration_ms,
            timed_out=stage.timed_out,
            diagnostic=stage.diagnostic,
        )

        return JsonResult(ok=True, payload=answer.model_dump(mode="json"))

    @staticmethod
    def _failed(head: StdoutHead, error: Exception) -> dict[str, object]:
        """Голова вывода остаётся в ответе: команда успела что-то напечатать."""
        answer = BashFailureAnswer(
            stdout=head.text(),
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
