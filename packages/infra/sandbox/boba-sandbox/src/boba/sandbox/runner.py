"""Запуск команды в песочнице: одна точка входа для всех инструментов."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import sys
from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

from boba.sandbox.argv import build_bwrap_argv
from boba.sandbox.cgroup import CgroupManager, GroupLimits
from boba.sandbox.diagnostics import SandboxDiagnostics
from boba.sandbox.process_runner import RunResult, run_subprocess
from boba.sandbox.profile import BindSpec, SandboxProfile
from boba.toolkit.launcher import LaunchOutcome
from boba.workspace import build_chain_argv, require_fuse
from boba.workspace.launcher import (
    EXIT_MOUNT_ERROR,
    LAUNCHER_ERROR_PREFIX,
    LAUNCHER_LOG_PREFIX,
    ResourceLimits,
)


def has_bwrap() -> bool:
    """Есть ли bubblewrap в PATH: без него песочницу не поднять."""
    return shutil.which("bwrap") is not None


SandboxOutcome = LaunchOutcome
"""Историческое имя результата запуска; тип задаёт порт."""

__all__ = ["SandboxOutcome", "SandboxRunner", "ToolCallContext", "has_bwrap"]

logger = logging.getLogger(__name__)


class SandboxRunner:
    """Выполняет команду в уже собранном профиле песочницы."""

    FAIL_TAIL_CHARS: ClassVar[int] = 2000

    def __init__(
        self,
        tool: str,
        profile: SandboxProfile,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> None:
        self._tool = tool
        self._profile = profile
        self._path_vars = path_vars

    def run(
        self,
        command: str,
        stdin: str,
        stdout_sink: Callable[[bytes], None] | None = None,
    ) -> SandboxOutcome:
        rendered = self._profile.render(dict(self._path_vars()))
        self._prepare_dirs(rendered)

        limits = self.limits_of(rendered)
        argv, runner_limits = self._build_argv(rendered, command, limits)

        name = self._label()
        self._log_start(name, rendered, limits, command)
        group = GroupLimits.of_profile(rendered)
        cgroup_dir: str | None = None
        manager: CgroupManager | None = None
        if group.requested:
            manager = CgroupManager(rendered.cgroup_base)
            cgroup_dir = manager.acquire(group)
            logger.info(
                "sandbox[%s]: cgroup %s (%s)", name, cgroup_dir, group.describe()
            )
        try:
            result = run_subprocess(
                argv,
                stdin_data=stdin.encode("utf-8"),
                timeout_sec=rendered.timeout_sec,
                max_output_bytes=rendered.max_output_bytes,
                cwd="/",
                env=os.environ,
                stdout_sink=stdout_sink,
                limits=runner_limits,
                cgroup_dir=cgroup_dir,
            )
        finally:
            if manager is not None and cgroup_dir is not None:
                manager.release(cgroup_dir)
        result = self._drain_launcher_log(name, result)
        self._log_finish(name, result)
        if result.timed_out or result.exit_code != 0:
            self._log_failure(name, result)
        self._raise_on_mount_error(result)

        diagnostic = SandboxDiagnostics.explain(result, rendered)
        if diagnostic:
            logger.warning("sandbox[%s]: %s", name, diagnostic)
        return SandboxOutcome(name, result, diagnostic)

    def _label(self) -> str:
        """Профиль плюс имя инструмента: sandbox[confluence:confluence_search]."""
        call = ToolCallContext.get()
        if call and call != self._tool:
            return f"{self._tool}:{call}"
        return self._tool

    @staticmethod
    def limits_of(profile: SandboxProfile) -> ResourceLimits:
        return ResourceLimits(
            max_memory_bytes=profile.max_memory_bytes,
            max_cpu_sec=profile.max_cpu_sec,
            max_file_size_bytes=profile.max_file_size_bytes,
            max_open_files=profile.max_open_files,
            oom_score_adj=profile.oom_score_adj,
        )

    @staticmethod
    def _prepare_dirs(profile: SandboxProfile) -> None:
        for spec in profile.rw_binds:
            Path(spec.host).mkdir(parents=True, exist_ok=True)
        for spec in profile.rw_images:
            Path(spec.host).parent.mkdir(parents=True, exist_ok=True)

    def _build_argv(
        self,
        profile: SandboxProfile,
        command: str,
        limits: ResourceLimits,
    ) -> tuple[list[str], ResourceLimits | None]:
        if not profile.rw_images:
            argv = build_bwrap_argv(profile, command, env=profile.env_set)
            return argv, limits

        require_fuse()
        mounts: list[BindSpec] = []
        images: list[tuple[str, str]] = []
        rw_paths: list[str] = []
        for spec in profile.rw_images:
            mnt = f"{spec.host}.mnt"
            images.append((spec.host, mnt))
            mounts.append(BindSpec(host=mnt, target=spec.target))
        for spec in profile.rw_binds:
            rw_paths.append(spec.host)

        inner = profile.model_copy(
            update={"rw_binds": profile.rw_binds + tuple(mounts)},
        )
        inner_argv = build_bwrap_argv(inner, command, env=inner.env_set, nested=True)
        argv = build_chain_argv(
            images=images,
            template=profile.image_template,
            op=["run", shlex.join(inner_argv)],
            python_bin=sys.executable,
            options=profile.launcher.to_options(),
            limits=limits,
            rw_paths=rw_paths,
            network=profile.network,
        )
        # лимиты применяет лаунчер к самой команде; обвязку не душим
        return argv, None

    @staticmethod
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
            "processes=%s timeout=%ss output=%sB oom_score_adj=%s group=[%s]",
            profile,
            limits.max_memory_bytes,
            limits.max_cpu_sec,
            limits.max_file_size_bytes,
            limits.max_open_files,
            rendered.max_processes,
            rendered.timeout_sec,
            rendered.max_output_bytes,
            limits.oom_score_adj,
            GroupLimits.of_profile(rendered).describe(),
        )
        logger.info("sandbox[%s]: command %r", profile, command)

    @staticmethod
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

    @classmethod
    def _log_failure(cls, profile: str, result: RunResult) -> None:
        """Причина падения в лог: без неё rc=1 не говорит ничего."""
        if result.timed_out:
            reason = f"timed out after {result.duration_ms}ms"
        else:
            reason = f"rc={result.exit_code}"
        tail = cls._tail(result.stderr)
        if not tail:
            tail = cls._tail(result.stdout)
        if not tail:
            tail = "<no output>"
        logger.warning(
            "sandbox[%s]: failed (%s); output tail: %s", profile, reason, tail
        )

    @classmethod
    def _tail(cls, text: str) -> str:
        stripped = text.strip()
        if len(stripped) <= cls.FAIL_TAIL_CHARS:
            return stripped
        return "…" + stripped[-cls.FAIL_TAIL_CHARS :]

    @staticmethod
    def _drain_launcher_log(profile: str, result: RunResult) -> RunResult:
        """Строки лаунчера уходят в лог: в stderr остаётся вывод команды."""
        if LAUNCHER_LOG_PREFIX not in result.stderr:
            return result
        kept: list[str] = []
        for line in result.stderr.splitlines():
            if line.startswith(LAUNCHER_LOG_PREFIX):
                logger.info(
                    "sandbox[%s]: %s", profile, line[len(LAUNCHER_LOG_PREFIX) :]
                )
            else:
                kept.append(line)
        stderr = "\n".join(kept)
        if kept and result.stderr.endswith("\n"):
            stderr += "\n"
        return replace(result, stderr=stderr)

    @staticmethod
    def _raise_on_mount_error(result: RunResult) -> None:
        if result.exit_code != EXIT_MOUNT_ERROR:
            return
        if LAUNCHER_ERROR_PREFIX not in result.stderr:
            return
        msg = f"sandbox: image not mounted: {result.stderr.strip()}"
        raise RuntimeError(msg)


class ToolCallContext:
    """Имя langchain-инструмента в текущем контексте выполнения."""

    _name: ClassVar[ContextVar[str]] = ContextVar("tool_call_name", default="")

    @classmethod
    def set(cls, name: str) -> Token[str]:
        return cls._name.set(name)

    @classmethod
    def reset(cls, token: Token[str]) -> None:
        cls._name.reset(token)

    @classmethod
    def get(cls) -> str:
        return cls._name.get()
