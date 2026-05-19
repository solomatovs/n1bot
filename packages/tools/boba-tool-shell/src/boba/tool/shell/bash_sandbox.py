"""`bash_sandbox`: запуск команды в bubblewrap-песочнице.

LLM передаёт `command` (обязательное), `stdin` (опционально, по умолчанию
пустая строка) и `profile` (опционально, имя из реестра профилей).
Поля профиля менять LLM не может.

Безопасность Python-стороны: `command` передаётся как единичный argv-
элемент в `bash -c`, без shell-интерполяции на стороне Python. Все
остальные argv-элементы (пути для bind/ro-bind, env-вары, cwd) —
operator-controlled через конфиг. Шелл-интерпретация `command`
происходит внутри bwrap-песочницы под изоляцией.
"""

from __future__ import annotations

import os
import shutil
from typing import Annotated, Any

from pydantic import Field

from boba.tool.shell._runner import RunResult, run_subprocess
from boba.tool.shell._sandbox import build_bwrap_argv
from boba.tool.shell.config import BashSandboxConfig
from boba.tools import FromConfig, tool

__all__ = ["bash_sandbox"]


_MAX_COMMAND_LEN = 16_384
_MAX_STDIN_LEN = 1 * 1024 * 1024  # 1 MiB


def _has_bwrap() -> bool:
    """Platform-check: `bwrap` присутствует в PATH.

    Если bubblewrap не установлен на хосте — tool не регистрируется на
    module-load: `bash_sandbox` остаётся обычной функцией без `@tool`-
    маркера, и `AgentBuilder._scan_module` его пропустит.
    """
    return shutil.which("bwrap") is not None


def bash_sandbox(
    command: Annotated[
        str,
        Field(
            min_length=1,
            max_length=_MAX_COMMAND_LEN,
            description="Shell-команда (передаётся в `bash -c`).",
        ),
    ],
    cfg: Annotated[BashSandboxConfig, FromConfig()],
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
) -> dict[str, Any]:
    """Выполнить shell-команду через `bash -c` в bubblewrap-песочнице.

    Команда выполняется в bwrap: пользователь, PID, IPC, UTS, сеть (если
    выключена в профиле) — всё в отдельных namespace'ах. Доступ к файловой
    системе ограничен выбранным профилем.
    """
    profile_name = profile or cfg.default_profile
    profile_dto = cfg.profiles.get(profile_name)
    if profile_dto is None:
        return _unknown_profile_payload(profile_name, sorted(cfg.profiles))

    workspace_root = str(cfg.workspace_root)
    argv = build_bwrap_argv(
        profile_dto,
        command,
        workspace_root=workspace_root,
        env=profile_dto.env_set,
    )
    result = run_subprocess(
        argv,
        stdin_data=stdin.encode("utf-8"),
        timeout_sec=profile_dto.timeout_sec,
        max_output_bytes=profile_dto.max_output_bytes,
        # bwrap внутри делает --chdir/--clearenv, но Popen всё равно
        # требует валидные cwd/env для запуска самого bwrap.
        cwd=workspace_root,
        env=os.environ,
    )
    return _result_to_payload(result, profile_name)


def _unknown_profile_payload(name: str, available: list[str]) -> dict[str, Any]:
    return {
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


if _has_bwrap():
    bash_sandbox = tool(bash_sandbox)
