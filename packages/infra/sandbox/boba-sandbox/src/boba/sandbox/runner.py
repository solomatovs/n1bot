"""Кадры журнала песочницы и признаки её окружения.

Запуск инструментов живёт в boba.sandbox.zygote; здесь остаётся то, чем
пользуются все пути запуска: релей структурных кадров payload'а и лаунчера в
журнал приложения, тройник stderr, описание упавшего запуска, журнал
жизненного цикла зиготы и проверка наличия bwrap.

Ошибки:
SandboxMountError — образ вызова не смонтирован, команда не запускалась.
SandboxLaunchError — окружение запуска подготовить не удалось.
SandboxChainError — корень секции отвалился, вызов не запускался.
Все три — LauncherError: других типов слой наружу не выпускает.
"""

from __future__ import annotations

import json
import logging
import signal
import time
import uuid
from collections.abc import Sequence
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.sandbox.profile import SandboxProfile
from boba.toolkit.binaries import SandboxBinary
from boba.toolkit.channels import JournalChannel, WrapChannel
from boba.toolkit.images import LauncherMarker
from boba.toolkit.launcher import LauncherError, LaunchPayload, RunResult
from boba.toolkit.stream import (
    ChannelSinks,
    Chunk,
)


def has_bwrap(profile: SandboxProfile) -> bool:
    """Есть ли bubblewrap в доверенных каталогах: без него песочницу не поднять."""
    return profile.host.binaries.has(SandboxBinary.BWRAP)


__all__ = [
    "DeathReport",
    "FailureLog",
    "IncidentReason",
    "LifecycleJournal",
    "LifecyclePhase",
    "SandboxChainError",
    "SandboxLaunchError",
    "SandboxLogRelay",
    "SandboxMountError",
    "StderrTee",
    "has_bwrap",
]

logger = logging.getLogger(__name__)


class SandboxMountError(LauncherError):
    """Образ не смонтирован: команда не запускалась, результата нет."""


class SandboxLaunchError(LauncherError):
    """Окружение запуска подготовить не удалось: команда не запускалась."""


class SandboxChainError(LauncherError):
    """Корень секции не смонтирован: вызов не запускался, секция встаёт заново."""


class LifecyclePhase(StrEnum):
    """Фазы восстановления: по ним журнал читается одной цепочкой."""

    DETECTED = "detected"
    RECOVERY = "recovery"
    ATTEMPT = "attempt"
    SPAWNED = "spawned"
    READY = "ready"
    FAILED = "failed"
    GAVE_UP = "gave-up"
    STOPPED = "stopped"


class IncidentReason(StrEnum):
    """Из-за чего началось восстановление."""

    START = "first start"
    DIED = "process died"
    CHAIN_LOST = "root mount lost"


class Elapsed:
    """Длительность для журнала: секунды до минуты, дальше — минуты и часы."""

    MINUTE: ClassVar[float] = 60.0
    HOUR: ClassVar[float] = 3600.0

    @classmethod
    def of(cls, seconds: float) -> str:
        if seconds < cls.MINUTE:
            return f"{seconds:.1f}s"

        if seconds < cls.HOUR:
            minutes = int(seconds // cls.MINUTE)
            rest = int(seconds - minutes * cls.MINUTE)

            return f"{minutes}m{rest:02d}s"

        hours = int(seconds // cls.HOUR)
        left = seconds - hours * cls.HOUR

        return f"{hours}h{int(left // cls.MINUTE):02d}m"


class DeathReport(BaseModel):
    """Что известно об умершей зиготе: без этого rc в журнале не говорит ничего."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pid: int
    exit_code: int | None = Field(
        default=None,
        description="Код выхода; отрицательный — номер убившего сигнала.",
    )
    uptime_sec: float = Field(ge=0)
    calls_total: int = Field(ge=0)
    in_flight: tuple[str, ...] = ()
    """Идентификаторы вызовов, застигнутых смертью: они получили ошибку."""
    stderr_tail: str = ""

    def describe(self) -> str:
        """Строка отчёта: причина, время жизни, счётчики, последние слова."""
        parts = [
            f"pid={self.pid}",
            self._exit(),
            f"uptime {Elapsed.of(self.uptime_sec)}",
        ]
        parts.append(f"served {self.calls_total} calls")

        if self.in_flight:
            parts.append(
                f"{len(self.in_flight)} in flight ({', '.join(self.in_flight)})"
            )

        line = ", ".join(parts)
        if not self.stderr_tail:
            return f"{line}; no stderr"

        return f"{line}; stderr tail: {self.stderr_tail}"

    def _exit(self) -> str:
        if self.exit_code is None:
            return "still running"

        if self.exit_code >= 0:
            return f"rc={self.exit_code}"

        try:
            name = signal.Signals(-self.exit_code).name
        except ValueError:
            return f"rc={self.exit_code}"

        return f"killed by {name} (rc={self.exit_code})"


class LifecycleJournal:
    """Журнал жизненного цикла зиготы: инцидент, попытки и итог одной цепочкой.

    Каждая строка несёт имя секции и короткий идентификатор инцидента, чтобы
    восстановление собиралось из журнала приложения одним grep'ом.
    """

    ID_CHARS: ClassVar[int] = 6

    def __init__(self, section: str) -> None:
        self._section = section
        self._incident = ""
        self._opened = 0.0

    @property
    def incident(self) -> str:
        """Идентификатор открытого инцидента; пустая строка — инцидента нет."""
        return self._incident

    def open(self, reason: IncidentReason, detail: str = "") -> str:
        """Начало инцидента: дальше все строки идут под этим идентификатором."""
        self._incident = uuid.uuid4().hex[: self.ID_CHARS]
        self._opened = time.monotonic()

        text = f"recovery started: reason={reason.value}"
        if detail:
            text = f"{text}, {detail}"

        self._line(LifecyclePhase.RECOVERY, text, logging.WARNING)

        return self._incident

    def death(self, report: DeathReport) -> None:
        """Отчёт о смерти: он объясняет, почему восстановление вообще началось."""
        self._line(LifecyclePhase.DETECTED, report.describe(), logging.WARNING)

    def attempt(self, number: int, total: int, backoff_sec: float) -> None:
        text = f"attempt {number}/{total} after {Elapsed.of(backoff_sec)} backoff"
        self._line(LifecyclePhase.ATTEMPT, text, logging.INFO)

    def spawned(self, pid: int, root: str, modules: Sequence[str]) -> None:
        listed = ", ".join(modules)
        if not listed:
            listed = "none"

        text = f"pid={pid}, root={root}, modules={listed}"
        self._line(LifecyclePhase.SPAWNED, text, logging.INFO)

    def ready(self, pid: int, warmup_ms: int) -> None:
        """Итог восстановления: сколько заняло целиком и сколько — прогрев."""
        text = (
            f"pid={pid}, warmup {Elapsed.of(warmup_ms / 1000)}, "
            f"section is serving again after {Elapsed.of(self._since())}"
        )
        self._line(LifecyclePhase.READY, text, logging.WARNING)
        self._incident = ""

    def failed(self, cause: str, tail: str) -> None:
        text = f"attempt failed: {cause}"
        if tail:
            text = f"{text}; stderr tail: {tail}"

        self._line(LifecyclePhase.FAILED, text, logging.WARNING)

    def gave_up(self, attempts: int) -> None:
        text = (
            f"gave up after {attempts} attempt(s) in {Elapsed.of(self._since())}: "
            f"the section stays unavailable until the app restarts"
        )
        self._line(LifecyclePhase.GAVE_UP, text, logging.ERROR)
        self._incident = ""

    def stopped(self, report: DeathReport) -> None:
        """Штатная остановка: тот же отчёт, но это не инцидент."""
        self._line(LifecyclePhase.STOPPED, report.describe(), logging.INFO)
        self._incident = ""

    def _since(self) -> float:
        if not self._opened:
            return 0.0

        return time.monotonic() - self._opened

    def _line(self, phase: LifecyclePhase, text: str, level: int) -> None:
        incident = self._incident
        if not incident:
            incident = "-"

        logger.log(
            level, "zygote[%s] %s %s: %s", self._section, incident, phase.value, text
        )


class FailureLog:
    """Описание упавшего запуска для журнала: причина и хвост вывода."""

    NO_OUTPUT: ClassVar[str] = "<no output>"

    @classmethod
    def describe(cls, result: RunResult, limit: int) -> str:
        """Без причины и хвоста rc=1 в журнале не говорит ничего."""
        if result.timed_out:
            reason = f"timed out after {result.duration_ms}ms"
        else:
            reason = f"rc={result.exit_code}"

        tail = cls.tail(result.stderr, limit)
        if not tail:
            tail = cls.tail(result.stdout, limit)

        if not tail:
            tail = cls.NO_OUTPUT

        return f"failed ({reason}); output tail: {tail}"

    @classmethod
    def tail(cls, text: str, limit: int) -> str:
        """Хвост вывода: начало обрезано, обрезка помечена многоточием."""
        stripped = text.strip()
        if len(stripped) <= limit:
            return stripped

        return f"…{stripped[-limit:]}"


class StderrTee:
    """Строки stderr процесса в журнал вызова: обвязка и тело разными каналами.

    Кадры лаунчера образов всегда едут каналом обвязки (wrap_stderr). Прочие
    строки — сырой stderr, и чей он, знает только вызывающий: у канального
    запуска тело пишет своим дескриптором, поэтому в сыром stderr остаётся
    та же обвязка, а у текстового по нему говорит само тело инструмента.

    Рекордер канала открывается первой строкой — молчаливый запуск лишнего
    файла в журнале не оставляет.
    """

    def __init__(self, sinks: ChannelSinks | None, raw: JournalChannel) -> None:
        self._sinks = sinks
        self._raw = raw

    def wrap(self, line: str) -> None:
        """Строка обвязки запуска: лаунчер образов, bwrap."""
        self._write(WrapChannel.STDERR, line)

    def raw(self, line: str) -> None:
        """Строка сырого stderr процесса: канал задан при сборке."""
        self._write(self._raw, line)

    def _write(self, channel: JournalChannel, line: str) -> None:
        if self._sinks is None:
            return

        sink = self._sinks.sink_of(channel)
        sink.feed_text(f"{line}\n")


class SandboxLogRelay:
    """Кадры `sandbox-log:` из stderr в общий журнал: инструмент и уровень.

    Сырые строки stderr в журнал приложения не идут — их пишет журнал вызова
    (tee); здесь остаются только структурные кадры payload'а и лаунчера.

    Пользователь в записи попадает сам — его подставляет фабрика записей
    приложения, а релей работает в контексте вызвавшего инструмента.
    """

    NOISE_LEVEL: ClassVar[int] = logging.DEBUG
    """Уровень для битых кадров лога: аномалия payload'а, не вывод."""

    def __init__(self, label: str, tee: StderrTee) -> None:
        self._label = label
        self._tee = tee
        self._tail = bytearray()

    def feed(self, data: Chunk) -> None:
        """Приём байт по мере чтения: долгий инструмент не молчит до конца."""
        self._tail.extend(data)
        while True:
            index = self._tail.find(b"\n")
            if index < 0:
                return
            line = self._tail[:index].decode("utf-8", errors="replace")
            del self._tail[: index + 1]
            self._line(line)

    def flush(self) -> None:
        """Последняя строка без перевода тоже должна попасть в журнал."""
        if not self._tail:
            return
        line = self._tail.decode("utf-8", errors="replace")
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
            self._tee.wrap(body)
            return
        if line.strip():
            self._tee.raw(line)

    def _log_frame(self, body: str) -> None:
        """Кадр лога payload'а — голос тела инструмента, а не обвязки."""
        try:
            frame = json.loads(body)
        except json.JSONDecodeError:
            logger.log(self.NOISE_LEVEL, "tool[%s]: %s", self._label, body)
            self._tee.raw(body)
            return
        if not isinstance(frame, dict):
            logger.log(self.NOISE_LEVEL, "tool[%s]: %s", self._label, body)
            self._tee.raw(body)
            return
        level = self._level_of(str(frame.get("lvl") or ""))
        name = str(frame.get("name") or "?")
        message = str(frame.get("msg") or "")
        logger.log(level, "tool[%s] %s: %s", self._label, name, message)
        self._tee.raw(f"{name}: {message}")

    @staticmethod
    def _level_of(name: str) -> int:
        resolved = logging.getLevelName(name.upper())
        if isinstance(resolved, int):
            return resolved
        return logging.INFO
