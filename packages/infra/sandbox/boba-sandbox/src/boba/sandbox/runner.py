"""Запуск команды в песочнице: одна точка входа для всех инструментов."""

from __future__ import annotations

import json
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
from boba.toolkit.launcher import LaunchOutcome, LaunchPayload
from boba.toolkit.payload import PayloadLogging
from boba.toolkit.stream import ToolStreamBuffer, ToolStreamTap
from boba.workspace.launcher import (
    LauncherExit,
    LauncherMarker,
    LauncherMode,
    ResourceLimits,
    build_chain_argv,
    require_fuse,
)


def has_bwrap() -> bool:
    """Есть ли bubblewrap в PATH: без него песочницу не поднять."""
    return shutil.which("bwrap") is not None


SandboxOutcome = LaunchOutcome
"""Историческое имя результата запуска; тип задаёт порт."""

__all__ = [
    "SandboxLogRelay",
    "SandboxOutcome",
    "SandboxRunner",
    "ToolCallContext",
    "has_bwrap",
]

logger = logging.getLogger(__name__)


class SandboxLogRelay:
    """Логи payload'а из stderr в общий журнал: видно инструмент и уровень.

    Пользователь в записи попадает сам — его подставляет фабрика записей
    приложения, а релей работает в контексте вызвавшего инструмента.
    """

    NOISE_LEVEL: ClassVar[int] = logging.DEBUG
    """Уровень для сырых строк stderr: варнинги библиотек, трейсбеки."""

    def __init__(self, label: str, tee: Callable[[str], None]) -> None:
        self._label = label
        self._tee = tee
        self._tail = bytearray()

    def feed(self, data: bytes) -> None:
        """Приём байт по мере чтения: долгий инструмент не молчит до конца."""
        self._tail.extend(data)
        while True:
            index = self._tail.find(b"\n")
            if index < 0:
                return
            line = bytes(self._tail[:index]).decode("utf-8", errors="replace")
            del self._tail[: index + 1]
            self._line(line)

    def flush(self) -> None:
        """Последняя строка без перевода тоже должна попасть в журнал."""
        if not self._tail:
            return
        line = bytes(self._tail).decode("utf-8", errors="replace")
        self._tail.clear()
        self._line(line)

    @classmethod
    def relayed(cls, line: str) -> bool:
        """Строка уже ушла в журнал: в stderr результата её держать незачем."""
        return line.startswith((LaunchPayload.LOG_MARKER, LauncherMarker.LOG.value))

    def _line(self, line: str) -> None:
        if line.startswith(LaunchPayload.LOG_MARKER):
            self._log_frame(line[len(LaunchPayload.LOG_MARKER) :])
            return
        if line.startswith(LauncherMarker.LOG.value):
            body = line[len(LauncherMarker.LOG) :]
            logger.info("sandbox[%s]: %s", self._label, body)
            self._tee(body)
            return
        if line.strip():
            logger.log(self.NOISE_LEVEL, "tool[%s]: %s", self._label, line)
            self._tee(line)

    def _log_frame(self, body: str) -> None:
        try:
            frame = json.loads(body)
        except json.JSONDecodeError:
            logger.log(self.NOISE_LEVEL, "tool[%s]: %s", self._label, body)
            self._tee(body)
            return
        if not isinstance(frame, dict):
            logger.log(self.NOISE_LEVEL, "tool[%s]: %s", self._label, body)
            self._tee(body)
            return
        level = self._level_of(str(frame.get("lvl") or ""))
        name = str(frame.get("name") or "?")
        message = str(frame.get("msg") or "")
        logger.log(level, "tool[%s] %s: %s", self._label, name, message)
        self._tee(f"{name}: {message}")

    @staticmethod
    def _level_of(name: str) -> int:
        resolved = logging.getLevelName(name.upper())
        if isinstance(resolved, int):
            return resolved
        return logging.INFO


class SandboxRunner:
    """Выполняет команду в уже собранном профиле песочницы."""

    FAIL_TAIL_CHARS: ClassVar[int] = 2000

    APP_LOGGER: ClassVar[str] = "boba"
    """Чей уровень наследует payload: настройка живёт в конфиге приложения."""

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
        tap = ToolStreamTap.get()
        relay = SandboxLogRelay(name, self._stream_tee(tap))
        self._log_start(name, rendered, limits, command)

        # сырой stdout едет в окно живого вывода только у текстового запуска:
        # у потокового stdout занят кадрами, их окно получает после декодера
        out_sink = stdout_sink
        keep_stdout = stdout_sink is None
        if tap is not None and stdout_sink is None:
            out_sink = tap.feed
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
                stdout_sink=out_sink,
                keep_stdout=keep_stdout,
                stderr_sink=relay.feed,
                limits=runner_limits,
                cgroup_dir=cgroup_dir,
            )
        finally:
            relay.flush()
            if manager is not None and cgroup_dir is not None:
                manager.release(cgroup_dir)
        result = self._drop_relayed(result)
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
    def _stream_tee(tap: ToolStreamBuffer | None) -> Callable[[str], None]:
        """Строки stderr в окно живого вывода; без тапа — некуда."""

        def tee(line: str) -> None:
            if tap is None:
                return
            tap.feed_text(line + "\n")

        return tee

    @staticmethod
    def _env_of(profile: SandboxProfile) -> dict[str, str]:
        """env профиля плюс уровень логов: он берётся из секции logger приложения."""
        env = dict(profile.env_set)
        level = logging.getLogger(SandboxRunner.APP_LOGGER).getEffectiveLevel()
        env[PayloadLogging.LEVEL_ENV] = logging.getLevelName(level)
        return env

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
        env = self._env_of(profile)
        if not profile.rw_images:
            argv = build_bwrap_argv(profile, command, env=env)
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
        inner_argv = build_bwrap_argv(inner, command, env=env, nested=True)
        argv = build_chain_argv(
            images=images,
            template=profile.image_template,
            op=[LauncherMode.RUN.value, shlex.join(inner_argv)],
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
    def _drop_relayed(result: RunResult) -> RunResult:
        """Залогированное релеем убираем: в stderr остаётся объяснение падения."""
        kept: list[str] = []
        for line in result.stderr.splitlines():
            if SandboxLogRelay.relayed(line):
                continue
            kept.append(line)
        stderr = "\n".join(kept)
        if kept and result.stderr.endswith("\n"):
            stderr += "\n"
        return replace(result, stderr=stderr)

    @staticmethod
    def _raise_on_mount_error(result: RunResult) -> None:
        if result.exit_code != LauncherExit.MOUNT_ERROR:
            return
        if LauncherMarker.ERROR.value not in result.stderr:
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
