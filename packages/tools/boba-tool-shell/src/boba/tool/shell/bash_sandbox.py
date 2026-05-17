"""BashSandboxTool: запуск команды в bubblewrap-песочнице.

Tool сам отвечает за изоляцию — не зависит от `ProjectWorkspaceShell`
и других workspace-абстракций. На вход получает:
- готовый `workspace_root` (резолвится `ShellPlugin.build` на load-time);
- словарь профилей песочницы;
- имя default-профиля.

LLM передаёт `command` (обязательное), `stdin` (опционально, по умолчанию
пустая строка) и `profile` (опционально, имя из конфига). LLM не может
менять параметры профиля.

Безопасность Python-стороны: `command` от LLM передаётся как единичный
argv-элемент в `bash -c`, без какой-либо shell-интерполяции на стороне
Python. Все остальные argv-элементы (пути для bind/ro-bind, env-вары,
cwd) — operator-controlled через конфиг. Шелл-интерпретация `command`
происходит внутри bwrap-песочницы под изоляцией.
"""

from __future__ import annotations

import os
import shlex
import shutil
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.plugin.prompt import PromptOverlay
from boba.tool.shell._profile import SandboxProfile
from boba.tool.shell._runner import RunResult, run_subprocess
from boba.tool.shell._sandbox import build_bwrap_argv
from boba.tools.domain import (
    JsonResult,
    Tool,
    ToolContext,
    ToolName,
    ToolResult,
)

__all__ = ["BashSandboxArgs", "BashSandboxTool", "BashSandboxToolConfig"]


_MAX_COMMAND_LEN = 16_384
_MAX_STDIN_LEN = 1 * 1024 * 1024  # 1 MiB
_TOOL_NAME = ToolName("bash")


class BashSandboxArgs(BaseModel):
    """Выполнить shell-команду через `bash -c` в изолированной песочнице.

    Команда выполняется в bubblewrap: пользователь, PID, IPC, UTS, сеть
    (если выключена в профиле) — всё в отдельных namespace'ах. Доступ
    к файловой системе ограничен профилем.
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
    profile: str = Field(
        default="",
        description=(
            "Имя профиля песочницы. Пустая строка = взять default из конфига."
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


class BashSandboxToolConfig(BaseModel):
    """Конфиг tool 'bash_sandbox': prompt overlay + реестр профилей.

    Создаётся `ShellPlugin.build` из `SandboxToolConfig` + `workspace_root`.
    LLM выбирает профиль по имени, но не может менять его поля или
    `workspace_root`. Cross-field инвариант (default_profile ∈ profiles)
    валидируется здесь, чтобы конфиг был самопроверяемым.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: PromptOverlay = Field(default_factory=PromptOverlay)
    workspace_root: str = Field(min_length=1)
    profiles: dict[str, SandboxProfile] = Field(min_length=1)
    default_profile: str = Field(min_length=1)

    @staticmethod
    def _check_bwrap_available() -> None:
        if shutil.which("bwrap") is None:
            msg = (
                "ShellPlugin: bubblewrap (`bwrap`) не найден в PATH; "
                "требуется для tool `bash_sandbox`. Установи пакет "
                "(apt: bubblewrap) или исключи `bash_sandbox` из tools."
            )
            raise RuntimeError(msg)

    @model_validator(mode="after")
    def _check_default_profile_in_registry(self) -> Self:
        self._check_bwrap_available()

        if self.default_profile not in self.profiles:
            msg = (
                f"default_profile={self.default_profile!r} отсутствует в "
                f"profiles; доступные: {sorted(self.profiles)}"
            )
            raise ValueError(msg)
        return self


class BashSandboxTool(Tool[BashSandboxArgs, BashSandboxToolConfig]):
    """Sandboxed bash: выполнить команду внутри bwrap-песочницы."""

    def name(self) -> ToolName:
        return _TOOL_NAME

    def execute(self, ctx: ToolContext, req: BashSandboxArgs) -> ToolResult:
        del ctx
        cfg = self._cfg
        profile_name = req.profile or cfg.default_profile
        profile = cfg.profiles.get(profile_name)
        if profile is None:
            return _unknown_profile_payload(profile_name, sorted(cfg.profiles))

        argv = build_bwrap_argv(
            profile,
            req.command,
            workspace_root=cfg.workspace_root,
            env=profile.env_set,
        )
        result = run_subprocess(
            argv,
            stdin_data=req.stdin.encode("utf-8"),
            timeout_sec=profile.timeout_sec,
            max_output_bytes=profile.max_output_bytes,
            # bwrap внутри сам делает --chdir/--clearenv, но Popen всё
            # равно требует валидные cwd/env для запуска самого bwrap.
            cwd=cfg.workspace_root,
            env=os.environ,
        )
        return _result_to_json(result, profile_name)


def _unknown_profile_payload(name: str, available: list[str]) -> JsonResult:
    return JsonResult(payload={
        "exit_code": -1,
        "stdout": "",
        "stderr": (
            f"unknown sandbox profile: {name!r}; available: {available}"
        ),
        "duration_ms": 0,
        "truncated_stdout": False,
        "truncated_stderr": False,
        "timed_out": False,
        "profile": name,
        "error_kind": "unknown_profile",
    })


def _result_to_json(result: RunResult, profile: str) -> JsonResult:
    return JsonResult(payload={
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "truncated_stdout": result.truncated_stdout,
        "truncated_stderr": result.truncated_stderr,
        "timed_out": result.timed_out,
        "profile": profile,
    })
