"""BashTool: запуск shell-команды без bubblewrap-изоляции.

В отличие от `BashSandboxTool`, эта реализация запускает процесс
напрямую через `subprocess.Popen` — без namespace'ов, без mount-биндов,
с тем же доступом к ФС/сети, что и у самого агента. Использовать только
если хост-машине вы доверяете коду от LLM (например, dev-окружение,
где bubblewrap недоступен).

В local-варианте нет «профилей» — все operator-controlled параметры
(`cwd`, `env_passthrough`, `env_set`, `timeout_sec`, `max_output_bytes`)
живут плоско в конфиге плагина и передаются в `BashTool` единичными
значениями. LLM их менять не может.

Безопасность Python-стороны: `command` от LLM передаётся как единичный
argv-элемент в `bash -c`, без shell-интерполяции на стороне Python.
Шелл-интерпретация `command` — полная, под привилегиями агента, поэтому
tool опасен и должен быть включён только осознанно.
"""

from __future__ import annotations

import os
import shlex
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.plugin.prompt import PromptOverlay
from boba.tool.shell._profile_local import DEFAULT_PASSTHROUGH, resolve_local_env
from boba.tool.shell._runner import RunResult, run_subprocess
from boba.tools.domain import (
    JsonResult,
    Tool,
    ToolContext,
    ToolName,
    ToolResult,
)

__all__ = ["BashArgs", "BashTool", "BashToolConfig"]


_MAX_COMMAND_LEN = 16_384
_MAX_STDIN_LEN = 1 * 1024 * 1024  # 1 MiB
_BASH_BIN = "/bin/bash"
_TOOL_NAME = ToolName("bash")


class BashArgs(BaseModel):
    """
    Выполнить shell-команду через `bash -c`
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str = Field(
        min_length=1,
        max_length=_MAX_COMMAND_LEN,
        description="Shell-команда (передаётся в `bash -c`).",
    )
    stdin: str = Field(
        default="",
        max_length=_MAX_STDIN_LEN,
        description=(
            "Stdin для команды (UTF-8). Пустая строка = нет stdin."
        ),
    )

    @model_validator(mode="after")
    def _validate_command(self) -> Self:
        stripped = self.command.strip()
        if not stripped:
            msg = "command не может быть пустым/whitespace-only"
            raise ValueError(msg)
        try:
            shlex.split(stripped)
        except ValueError as exc:
            msg = (
                f"command содержит синтаксическую ошибку shell-токенизации "
                f"(несбалансированные кавычки/escape): {exc}"
            )
            raise ValueError(msg) from exc
        return self


class BashToolConfig(BaseModel):
    """Конфиг tool 'bash': prompt overlay + все policy-параметры.

    Создаётся `ShellPlugin.build` из `LocalToolConfig` + `workspace_root`.
    LLM эти поля менять не может — они operator-controlled. Range/format
    инварианты валидируются здесь (pydantic Field-constraints), а не в
    plugin'е — чтобы конфиг был самопроверяемым.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: PromptOverlay = Field(default_factory=PromptOverlay)
    workspace_root: str = Field(min_length=1)
    cwd: str = ""
    env_passthrough: tuple[str, ...] = DEFAULT_PASSTHROUGH
    env_set: dict[str, str] = Field(default_factory=dict)
    timeout_sec: int = Field(default=30, ge=1, le=3600)
    max_output_bytes: int = Field(default=256 * 1024, ge=1024)


class BashTool(Tool[BashArgs, BashToolConfig]):
    """Regular bash: выполнить команду напрямую на хосте (без bwrap)."""

    def name(self) -> ToolName:
        return _TOOL_NAME

    def execute(self, ctx: ToolContext, req: BashArgs) -> ToolResult:
        del ctx
        cfg = self._cfg
        argv = [_BASH_BIN, "-c", req.command]
        cwd = cfg.cwd or cfg.workspace_root
        env = resolve_local_env(cfg.env_passthrough, cfg.env_set, os.environ)
        result = run_subprocess(
            argv,
            stdin_data=req.stdin.encode("utf-8"),
            timeout_sec=cfg.timeout_sec,
            max_output_bytes=cfg.max_output_bytes,
            cwd=cwd,
            env=env,
        )
        return _result_to_json(result)


def _result_to_json(result: RunResult) -> JsonResult:
    return JsonResult(payload={
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "truncated_stdout": result.truncated_stdout,
        "truncated_stderr": result.truncated_stderr,
        "timed_out": result.timed_out,
    })
