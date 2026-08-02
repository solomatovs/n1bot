"""Tool bash: запуск команды в bubblewrap-песочнице.

command уходит единичным argv-элементом в bash -c — без shell-
интерполяции на стороне Python; пути, env и cwd задаёт только конфиг.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.chainlit2.agent.tools.process.runner import RunResult, run_subprocess
from boba.chainlit2.agent.tools.sandbox.argv import build_bwrap_argv
from boba.chainlit2.agent.tools.sandbox.config import BashSandboxConfig
from boba.chainlit2.agent.tools.sandbox.diagnostics import SandboxDiagnostics
from boba.chainlit2.agent.tools.sandbox.profile import BindSpec, SandboxProfile
from boba.chainlit2.rendering.render import pack_result
from boba.chainlit2.rendering.tool_result import JsonResult, ToolResult
from boba.chainlit2.workspace import build_chain_argv, require_fuse
from boba.chainlit2.workspace.launcher import (
    EXIT_MOUNT_ERROR,
    LAUNCHER_ERROR_PREFIX,
    LAUNCHER_LOG_PREFIX,
)
from boba.chainlit2.workspace.options import ResourceLimits

__all__ = ["build_bash_tool", "has_bwrap"]

logger = logging.getLogger(__name__)

_MAX_COMMAND_LEN = 16_384
_MAX_STDIN_LEN = 1 * 1024 * 1024
_LAUNCHER_PREFIX = LAUNCHER_LOG_PREFIX


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

        limits = ResourceLimits(
            max_memory_bytes=rendered.max_memory_bytes,
            max_cpu_sec=rendered.max_cpu_sec,
            max_file_size_bytes=rendered.max_file_size_bytes,
            max_open_files=rendered.max_open_files,
        )
        if rendered.rw_images:
            # лимиты применяет лаунчер к команде; обвязку (fuse2fs) не душим
            argv = _build_image_argv(rendered, command, limits)
            runner_limits = None
        else:
            argv = build_bwrap_argv(rendered, command, env=rendered.env_set)
            runner_limits = limits
        _log_start(profile_name, rendered, limits, command)
        stdin_data = stdin.encode("utf-8")
        result = run_subprocess(
            argv,
            stdin_data=stdin_data,
            timeout_sec=rendered.timeout_sec,
            max_output_bytes=rendered.max_output_bytes,
            cwd="/",
            env=os.environ,
            limits=runner_limits,
        )
        result = _drain_launcher_log(result, profile_name)
        _log_finish(profile_name, result)
        _raise_on_mount_error(result)
        diagnostic = SandboxDiagnostics.explain(
            result, rendered, _network_profiles(cfg)
        )
        if diagnostic:
            logger.warning("sandbox[%s]: %s", profile_name, diagnostic)
        return pack_result(
            JsonResult(
                ok=_succeeded(result),
                payload=_result_to_payload(result, profile_name, diagnostic),
            )
        )

    return bash


def _build_image_argv(
    rendered: SandboxProfile,
    command: str,
    limits: ResourceLimits,
) -> list[str]:
    require_fuse()

    mounts: list[BindSpec] = []
    images: list[tuple[str, str]] = []
    rw_paths: list[str] = []
    for spec in rendered.rw_images:
        Path(spec.host).parent.mkdir(parents=True, exist_ok=True)
        mnt = f"{spec.host}.mnt"
        images.append((spec.host, mnt))
        mounts.append(BindSpec(host=mnt, target=spec.target))
    for spec in rendered.rw_binds:
        rw_paths.append(spec.host)

    inner = rendered.model_copy(
        update={"rw_binds": rendered.rw_binds + tuple(mounts)},
    )
    inner_argv = build_bwrap_argv(inner, command, env=inner.env_set, nested=True)
    run_op = shlex.join(inner_argv)
    return build_chain_argv(
        images=images,
        template=rendered.image_template,
        op=["run", run_op],
        python_bin=sys.executable,
        options=rendered.launcher.to_options(),
        limits=limits,
        rw_paths=rw_paths,
        network=rendered.network,
    )


def _log_start(
    profile: str,
    rendered: SandboxProfile,
    limits: ResourceLimits,
    command: str,
) -> None:
    mounts: list[str] = []
    for spec in rendered.rw_images:
        mounts.append(f"{spec.host}(ext4)->{spec.target}")
    for spec in rendered.rw_binds:
        mounts.append(f"{spec.host}->{spec.target}")
    logger.info(
        "sandbox[%s]: start; rootfs=%r cwd=%r network=%s rw=%s",
        profile,
        rendered.rootfs,
        rendered.cwd,
        rendered.network,
        mounts,
    )
    logger.info(
        "sandbox[%s]: limits memory=%sB cpu=%ss file=%sB open_files=%s "
        "processes=%s timeout=%ss output=%sB",
        profile,
        limits.max_memory_bytes,
        limits.max_cpu_sec,
        limits.max_file_size_bytes,
        limits.max_open_files,
        rendered.max_processes,
        rendered.timeout_sec,
        rendered.max_output_bytes,
    )
    logger.info("sandbox[%s]: command %r", profile, command)


def _log_finish(profile: str, result: RunResult) -> None:
    logger.info(
        "sandbox[%s]: finished rc=%s in %sms timed_out=%s "
        "truncated_stdout=%s truncated_stderr=%s",
        profile,
        result.exit_code,
        result.duration_ms,
        result.timed_out,
        result.truncated_stdout,
        result.truncated_stderr,
    )


def _drain_launcher_log(result: RunResult, profile: str) -> RunResult:
    """Строки лаунчера уводит в лог: в stderr для LLM остаётся вывод команды."""
    if _LAUNCHER_PREFIX not in result.stderr:
        return result
    kept: list[str] = []
    for line in result.stderr.splitlines():
        if line.startswith(_LAUNCHER_PREFIX):
            logger.info("sandbox[%s]: %s", profile, line[len(_LAUNCHER_PREFIX) :])
        else:
            kept.append(line)
    stderr = "\n".join(kept)
    if kept and result.stderr.endswith("\n"):
        stderr += "\n"
    return replace(result, stderr=stderr)


def _raise_on_mount_error(result: RunResult) -> None:
    if result.exit_code != EXIT_MOUNT_ERROR:
        return
    if LAUNCHER_ERROR_PREFIX not in result.stderr:
        return
    msg = f"sandbox: image not mounted: {result.stderr.strip()}"
    raise RuntimeError(msg)


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
        "diagnostic": (
            f"Profile {name!r} is not defined in the configuration. "
            f"Available: {available}."
        ),
    }


def _network_profiles(cfg: BashSandboxConfig) -> tuple[str, ...]:
    names: list[str] = []
    for name, profile in cfg.profiles.items():
        if profile.network:
            names.append(name)
    return tuple(names)


def _result_to_payload(
    result: RunResult,
    profile: str,
    diagnostic: str,
) -> dict[str, Any]:
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "truncated_stdout": result.truncated_stdout,
        "truncated_stderr": result.truncated_stderr,
        "timed_out": result.timed_out,
        "profile": profile,
        "diagnostic": diagnostic,
    }
