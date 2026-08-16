"""Запуск команды в песочнице: одна точка входа для всех инструментов.

Ошибки: SandboxMountError — образ не смонтирован, команда не запускалась;
SandboxLaunchError — окружение запуска подготовить не удалось (лимиты, cgroup,
каталоги, доверенные бинари). Обе — LauncherError, других типов слой наружу
не выпускает; ToolStopped из остановки хода проходит как есть.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar

from boba.sandbox.argv import build_bwrap_argv
from boba.sandbox.cgroup import CgroupManager, GroupLimits
from boba.sandbox.channels import ChannelSet
from boba.sandbox.diagnostics import SandboxDiagnostics
from boba.sandbox.process_runner import RunResult, run_subprocess
from boba.sandbox.profile import BindSpec, SandboxProfile
from boba.toolkit.binaries import SandboxBinary
from boba.toolkit.cpu import CpuBudget
from boba.toolkit.launcher import LauncherError, LaunchOutcome, LaunchPayload
from boba.toolkit.payload import PayloadLogging
from boba.toolkit.stream import StreamSink, ToolCallContext, ToolStreamTap
from boba.workspace.launcher import (
    RO_MOUNT_ROOT,
    Launcher,
    LauncherExit,
    LauncherMarker,
    LauncherMode,
    ResourceLimits,
    build_chain_argv,
    require_fuse,
)


def has_bwrap(profile: SandboxProfile) -> bool:
    """Есть ли bubblewrap в доверенных каталогах: без него песочницу не поднять."""
    return profile.binaries.has(SandboxBinary.BWRAP)


SandboxOutcome = LaunchOutcome
"""Историческое имя результата запуска; тип задаёт порт."""

__all__ = [
    "SandboxLaunchError",
    "SandboxLogRelay",
    "SandboxMountError",
    "SandboxOutcome",
    "SandboxRunner",
    "has_bwrap",
]

logger = logging.getLogger(__name__)


class SandboxMountError(LauncherError):
    """Образ не смонтирован: команда не запускалась, результата нет."""


class SandboxLaunchError(LauncherError):
    """Окружение запуска подготовить не удалось: команда не запускалась."""


class SandboxLogRelay:
    """Кадры `sandbox-log:` из stderr в общий журнал: инструмент и уровень.

    Сырые строки stderr в журнал приложения не идут — их пишет журнал вывода
    инструмента (tee); здесь остаются только структурные кадры payload'а и
    лаунчера.

    Пользователь в записи попадает сам — его подставляет фабрика записей
    приложения, а релей работает в контексте вызвавшего инструмента.
    """

    NOISE_LEVEL: ClassVar[int] = logging.DEBUG
    """Уровень для битых кадров лога: аномалия payload'а, не вывод."""

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


@dataclass(frozen=True, slots=True)
class _ChainPlan:
    """Что монтирует цепочка лаунчера: ro-корень, rw-образы и их точки."""

    images: tuple[tuple[str, str], ...]
    ro_images: tuple[tuple[str, str], ...]
    inner: SandboxProfile
    rw_paths: tuple[str, ...]

    ROOTFS_MOUNT: ClassVar[str] = RO_MOUNT_ROOT
    """Точка корневого образа: tmpfs цепочки, каталог установки только читаем."""

    @classmethod
    def of(cls, profile: SandboxProfile) -> _ChainPlan:
        mounts: list[BindSpec] = []
        images: list[tuple[str, str]] = []
        for spec in profile.rw_images:
            mnt = f"{spec.host}.mnt"
            images.append((spec.host, mnt))
            mounts.append(BindSpec(host=mnt, target=spec.target))

        rw_paths: list[str] = []
        for spec in profile.rw_binds:
            rw_paths.append(spec.host)

        ro_images: list[tuple[str, str]] = []
        update: dict[str, object] = {"rw_binds": profile.rw_binds + tuple(mounts)}
        if profile.rootfs_image:
            ro_images.append((profile.rootfs_image, cls.ROOTFS_MOUNT))
            # внутренний bwrap видит корень уже смонтированным каталогом
            update["rootfs"] = cls.ROOTFS_MOUNT
            update["rootfs_image"] = ""

        return cls(
            images=tuple(images),
            ro_images=tuple(ro_images),
            inner=profile.model_copy(update=update),
            rw_paths=tuple(rw_paths),
        )


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
        """Граница слоя: наружу уходят только ошибки из контракта модуля."""
        try:
            return self._run(command, stdin, stdout_sink)
        except LauncherError:
            raise
        except Exception as exc:
            msg = f"sandbox[{self._label()}]: launch failed: {exc}"
            raise SandboxLaunchError(msg) from exc

    def run_with_channels(
        self,
        argv_tail: Sequence[str],
        stdin_data: bytes,
        channels: ChannelSet,
    ) -> SandboxOutcome:
        """Канальный запуск: команда под преамбулой редиректа, fd — насосу.

        Профиль с rw_images исполняется цепочкой лаунчера: дескрипторы
        каналов проезжают её насквозь — их номера едут лаунчеру env-списком
        (Launcher.PASS_FDS_ENV), и он передаёт их вложенному bwrap как есть.
        """
        try:
            return self._run_with_channels(argv_tail, stdin_data, channels)
        except LauncherError:
            raise
        except Exception as exc:
            msg = f"sandbox[{self._label()}]: launch failed: {exc}"
            raise SandboxLaunchError(msg) from exc

    def _channel_argv(
        self,
        rendered: SandboxProfile,
        argv_tail: Sequence[str],
        channels: ChannelSet,
        limits: ResourceLimits,
    ) -> tuple[list[str], ResourceLimits | None]:
        """Команда канального запуска: прямой bwrap либо цепочка лаунчера."""
        env = self._env_of(rendered)
        env.update(channels.env())

        inner = f"{channels.redirect()}; exec {shlex.join(argv_tail)}"

        if not rendered.rw_images and not rendered.rootfs_image:
            return build_bwrap_argv(rendered, inner, env=env), limits

        require_fuse(rendered.binaries)

        plan = _ChainPlan.of(rendered)
        inner_argv = build_bwrap_argv(plan.inner, inner, env=env, nested=True)

        pass_fds = ",".join(str(fd) for fd in channels.child_fds.values())
        argv = build_chain_argv(
            images=plan.images,
            ro_images=plan.ro_images,
            template=rendered.image_template,
            op=[LauncherMode.RUN.value, shlex.join(inner_argv)],
            python_bin=sys.executable,
            options=rendered.launcher.to_options(),
            limits=limits,
            binaries=rendered.binaries,
            extra_env={Launcher.PASS_FDS_ENV: pass_fds},
            rw_paths=plan.rw_paths,
            network=rendered.network,
        )
        # лимиты применяет лаунчер к самой команде; обвязку не душим
        return argv, None

    def _run_with_channels(
        self,
        argv_tail: Sequence[str],
        stdin_data: bytes,
        channels: ChannelSet,
    ) -> SandboxOutcome:
        rendered = self._profile.render(dict(self._path_vars()))

        self._prepare_dirs(rendered)
        limits = self.limits_of(rendered)

        argv, runner_limits = self._channel_argv(
            rendered, argv_tail, channels, limits
        )

        name = self._label()
        self._log_start(name, rendered, limits, shlex.join(argv_tail))

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
                stdin_data=stdin_data,
                timeout_sec=rendered.timeout_sec,
                cwd="/",
                env=os.environ,
                stdout_sink=None,
                keep_stdout=True,
                limits=runner_limits,
                cgroup_dir=cgroup_dir,
                pass_fds=tuple(channels.child_fds.values()),
                channel_sinks=channels.sinks(),
                on_spawn=channels.close_child_ends,
            )
        finally:
            channels.close()
            if manager is not None and cgroup_dir is not None:
                # счётчики читаются до удаления leaf'а: после release их нет
                self._log_throttling(name, manager, cgroup_dir)
                manager.release(cgroup_dir)

        self._log_finish(name, result)
        if result.timed_out or result.exit_code != 0:
            self._log_failure(name, result)

        diagnostic = SandboxDiagnostics.explain(result, rendered)
        if diagnostic:
            logger.warning("sandbox[%s]: %s", name, diagnostic)
        return SandboxOutcome(name, result, diagnostic)

    @staticmethod
    def _log_throttling(name: str, manager: CgroupManager, leaf: str) -> None:
        """Задушенный квотой запуск выглядит зависшим — это должно быть видно."""
        note = manager.throttling(leaf)
        if not note:
            return

        logger.warning("sandbox[%s]: %s", name, note)

    def diagnose(self, result: RunResult, tool_stderr: str) -> str:
        """Объяснение сбоя лимитами; в разборе участвует и хвост tool_stderr.

        У канального запуска stderr инструмента едет отдельным каналом и в
        result.stderr не попадает — без него маркеры памяти/лимитов не видны.
        """
        rendered = self._profile.render(dict(self._path_vars()))

        merged = replace(result, stderr=f"{result.stderr}\n{tool_stderr}")

        return SandboxDiagnostics.explain(merged, rendered)

    def _run(
        self,
        command: str,
        stdin: str,
        stdout_sink: Callable[[bytes], None] | None,
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
                self._log_throttling(name, manager, cgroup_dir)
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
        call = ToolCallContext.name()
        if call and call != self._tool:
            return f"{self._tool}:{call}"
        return self._tool

    @staticmethod
    def _stream_tee(tap: StreamSink | None) -> Callable[[str], None]:
        """Строки stderr в окно живого вывода; без тапа — некуда."""

        def tee(line: str) -> None:
            if tap is None:
                return
            tap.feed_text(line + "\n")

        return tee

    @staticmethod
    def _env_of(profile: SandboxProfile) -> dict[str, str]:
        """env профиля, уровень логов и доля CPU: их знает только хост.

        Нативные движки внутри считают ядра по хосту и не видят cgroup-квоту —
        без этой подсказки их потоки дерутся за выделенную долю.
        """
        env = dict(profile.env_set)

        level = logging.getLogger(SandboxRunner.APP_LOGGER).getEffectiveLevel()
        env[PayloadLogging.LEVEL_ENV] = logging.getLevelName(level)

        if cores := CpuBudget.of_percent(profile.cgroup_cpu_percent):
            env[CpuBudget.ENV_VAR] = str(cores)

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
        if not profile.rw_images and not profile.rootfs_image:
            argv = build_bwrap_argv(profile, command, env=env)
            return argv, limits

        require_fuse(profile.binaries)
        plan = _ChainPlan.of(profile)

        inner_argv = build_bwrap_argv(plan.inner, command, env=env, nested=True)
        argv = build_chain_argv(
            extra_env={},
            images=plan.images,
            ro_images=plan.ro_images,
            template=profile.image_template,
            op=[LauncherMode.RUN.value, shlex.join(inner_argv)],
            python_bin=sys.executable,
            options=profile.launcher.to_options(),
            limits=limits,
            binaries=profile.binaries,
            rw_paths=plan.rw_paths,
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

        root = rendered.rootfs
        if rendered.rootfs_image:
            root = f"{rendered.rootfs_image}(ext4)"

        logger.info(
            "sandbox[%s]: start; rootfs=%r cwd=%r network=%s rw=%s",
            profile,
            root,
            rendered.cwd,
            rendered.network,
            mounts,
        )
        logger.info(
            "sandbox[%s]: limits memory=%sB cpu=%ss file=%sB open_files=%s "
            "processes=%s timeout=%ss oom_score_adj=%s group=[%s]",
            profile,
            limits.max_memory_bytes,
            limits.max_cpu_sec,
            limits.max_file_size_bytes,
            limits.max_open_files,
            rendered.max_processes,
            rendered.timeout_sec,
            limits.oom_score_adj,
            GroupLimits.of_profile(rendered).describe(),
        )
        logger.info("sandbox[%s]: command %r", profile, command)

    @staticmethod
    def _log_finish(profile: str, result: RunResult) -> None:
        logger.info(
            "sandbox[%s]: finished rc=%s in %sms timed_out=%s",
            profile,
            result.exit_code,
            result.duration_ms,
            result.timed_out,
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
        raise SandboxMountError(msg)

