"""BashTool: запуск команды в bubblewrap-песочнице.

Tool сам отвечает за изоляцию — не зависит от `ProjectWorkspaceShell`
и других workspace-абстракций. На вход получает:
- готовый `workspace_root` (резолвится `ShellPlugin.build` на load-time);
- словарь профилей песочницы;
- имя default-профиля.

LLM передаёт `command` (обязательное), `stdin` (опционально) и
`profile` (опционально, имя из конфига). LLM не может менять параметры
профиля.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.plugin.prompt import PromptOverlay
from boba.tool.shell._profile import SandboxProfile
from boba.tool.shell._runner import RunResult, run_sandboxed
from boba.tool.shell._sandbox import build_bwrap_argv
from boba.tools.domain import (
    JsonResult,
    Tool,
    ToolContext,
    ToolResult,
    ToolSourceId,
)

__all__ = ["BashArgs", "BashTool", "BashToolConfig"]


_MAX_COMMAND_LEN = 16_384


class BashArgs(BaseModel):
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
    stdin: str | None = Field(
        default=None,
        description="Опциональный stdin для команды (UTF-8).",
    )
    profile: str | None = Field(
        default=None,
        description=(
            "Имя профиля песочницы. None — использовать default из конфига."
        ),
    )

    @model_validator(mode="after")
    def _strip_command(self) -> Self:
        if not self.command.strip():
            msg = "command не может быть пустым/whitespace-only"
            raise ValueError(msg)
        return self


@dataclass(frozen=True)
class BashToolConfig:
    """Конфиг tool 'bash': только prompt overlay (всё остальное в плагине)."""

    prompt: PromptOverlay


class BashTool(Tool[BashArgs, BashToolConfig]):
    """Sandboxed bash: выполнить команду внутри bwrap-песочницы."""

    def __init__(  # noqa: PLR0913
        self,
        cfg: BashToolConfig,
        ctx,
        source_id: ToolSourceId,
        workspace_root: str,
        profiles: dict[str, SandboxProfile],
        default_profile: str,
    ) -> None:
        super().__init__(cfg, ctx, source_id)
        self._workspace_root = workspace_root
        self._profiles = profiles
        self._default_profile = default_profile

    def execute(self, ctx: ToolContext, req: BashArgs) -> ToolResult:
        del ctx
        profile_name = req.profile or self._default_profile
        profile = self._profiles.get(profile_name)
        if profile is None:
            available = sorted(self._profiles)
            return JsonResult(
                payload={
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": (
                        f"unknown sandbox profile: {profile_name!r}; "
                        f"available: {available}"
                    ),
                    "duration_ms": 0,
                    "truncated_stdout": False,
                    "truncated_stderr": False,
                    "timed_out": False,
                    "profile": profile_name,
                    "error_kind": "unknown_profile",
                }
            )

        argv = build_bwrap_argv(
            profile,
            req.command,
            workspace_root=self._workspace_root,
            env=profile.env_set,
        )
        result = run_sandboxed(
            argv,
            stdin=req.stdin,
            timeout_sec=profile.timeout_sec,
            max_output_bytes=profile.max_output_bytes,
        )
        return _result_to_json(result, profile_name)


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
