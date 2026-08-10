"""Запуск команды в песочнице: каналы, насос и одна точка входа для всех инструментов.

Ошибки канального пути (SandboxChannels/ChannelPump/run_stage): LauncherError и
наследники (PayloadFailureError); исключения sink'ов пути данных и результата
проходят наверх как есть — их контракт задаёт потребитель. Старый кадровый путь
(run/ShellRunner) до этапа 3 сохраняет прежние
RuntimeError/ValueError/ShellRunnerInvariantError.
"""

from __future__ import annotations

import json
import logging
import os
import select
import selectors
import shlex
import shutil
import subprocess
import sys
import threading
import time
from abc import abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import ClassVar, Generic, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from boba.cancellation import TurnCancellation, current_cancellation
from boba.sandbox.argv import ChannelArgv, WrapArgsCodec, build_bwrap_argv
from boba.sandbox.cgroup import CgroupError, CgroupManager, GroupLimits
from boba.sandbox.diagnostics import SandboxDiagnostics
from boba.sandbox.profile import BindSpec, SandboxProfile
from boba.toolkit.channels import (
    Channel,
    ChannelError,
    LogFrame,
    ResultError,
    ResultFailure,
    ResultSuccess,
    ShellExit,
)
from boba.toolkit.launcher import (
    LauncherError,
    LaunchOutcome,
    LaunchPayload,
    PayloadFailureError,
    RunResult,
)
from boba.toolkit.payload import PayloadLogging
from boba.toolkit.stream import StreamSink, ToolStreamTap
from boba.workspace.launcher import (
    ChainChannelArgv,
    LauncherExit,
    LauncherMarker,
    LauncherMode,
    MountError,
    ResourceLimits,
    build_chain_argv,
    require_fuse,
)


def has_bwrap() -> bool:
    """Есть ли bubblewrap в PATH: без него песочницу не поднять."""
    return shutil.which("bwrap") is not None


SandboxOutcome = LaunchOutcome
"""Историческое имя результата запуска; тип задаёт порт."""

M = TypeVar("M", bound=BaseModel)

__all__ = [
    "ChannelPump",
    "ChannelSink",
    "LineSplitter",
    "RelaySink",
    "ResultSink",
    "SandboxChannels",
    "SandboxLogRelay",
    "SandboxOutcome",
    "SandboxRunner",
    "ShellRunner",
    "ShellRunnerInvariantError",
    "StageRunResult",
    "TailSink",
    "ToolCallContext",
    "has_bwrap",
]

logger = logging.getLogger(__name__)


class _ByteText(StrEnum):
    """Параметры декодирования байтовых каналов в текст."""

    ENCODING = "utf-8"
    ERRORS = "replace"


class LineSplitter:
    """Инкрементальное разбиение байтового потока на текстовые строки."""

    def __init__(self) -> None:
        self._tail = bytearray()

    def feed(self, data: bytes) -> Iterator[str]:
        """Полные строки из накопленного потока; хвост без \\n остаётся внутри."""
        self._tail.extend(data)

        lines: list[str] = []
        while True:
            index = self._tail.find(b"\n")
            if index < 0:
                break
            raw = bytes(self._tail[:index])
            del self._tail[: index + 1]
            lines.append(raw.decode(_ByteText.ENCODING, errors=_ByteText.ERRORS))

        return iter(lines)

    def flush(self) -> Iterator[str]:
        """Последняя строка без перевода; после вызова буфер пуст."""
        if not self._tail:
            return iter(())

        line = bytes(self._tail).decode(_ByteText.ENCODING, errors=_ByteText.ERRORS)
        self._tail.clear()

        return iter((line,))


class ChannelSink(Protocol):
    """Приёмник байтов одного канала."""

    @abstractmethod
    def feed(self, data: bytes) -> None:
        """Принять байты; исключение sink'а пути данных фатально для стадии."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Канал закончился (EOF) либо насос завершает работу."""
        ...


class TailSink(ChannelSink):
    """Ограниченный хвост канала в памяти: текст для диагностики и ошибок."""

    def __init__(self, max_bytes: int) -> None:
        if max_bytes <= 0:
            msg = f"max_bytes must be positive, got {max_bytes}"
            raise ValueError(msg)
        self._max_bytes = max_bytes
        self._buf = bytearray()
        self._truncated = False

    @property
    def truncated(self) -> bool:
        return self._truncated

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)

        overflow = len(self._buf) - self._max_bytes
        if overflow > 0:
            del self._buf[:overflow]
            self._truncated = True

    def close(self) -> None:
        """Хвост живёт до разбора итога стадии; закрывать нечего."""

    def text(self) -> str:
        return bytes(self._buf).decode(_ByteText.ENCODING, errors=_ByteText.ERRORS)


class RelaySink(ChannelSink):
    """Лог-кадры `tool_stderr` в журнал приложения, сырые строки — в raw-приёмник.

    Журнальный потребитель — побочная копия: собственная ошибка отключает релей
    пометкой в логе, поток стадии не рвётся.
    """

    NOISE_LEVEL: ClassVar[int] = logging.DEBUG
    """Уровень для битых кадров лога: аномалия payload'а, не вывод."""

    def __init__(self, label: str, raw: ChannelSink) -> None:
        self._label = label
        self._raw = raw
        self._lines = LineSplitter()
        self._disabled = False

    @staticmethod
    def level_of(name: str) -> int:
        resolved = logging.getLevelName(name.upper())
        if isinstance(resolved, int):
            return resolved
        return logging.INFO

    def feed(self, data: bytes) -> None:
        if self._disabled:
            return
        try:
            for line in self._lines.feed(data):
                self._line(line)
        except Exception:
            # побочная копия: релей отключается, поток стадии не рвётся
            self._disabled = True
            logger.exception("log relay [%s] failed; relay disabled", self._label)

    def close(self) -> None:
        if self._disabled:
            return
        try:
            for line in self._lines.flush():
                self._line(line)
        except Exception:
            self._disabled = True
            logger.exception("log relay [%s] failed on close", self._label)

    def _line(self, line: str) -> None:
        if not LogFrame.matches(line):
            self._raw_line(line)
            return

        try:
            frame = LogFrame.decode(line)
        except ChannelError:
            logger.log(self.NOISE_LEVEL, "tool[%s]: %s", self._label, line)
            self._raw_line(line)
            return

        level = self.level_of(frame.lvl)
        logger.log(level, "tool[%s] %s: %s", self._label, frame.name, frame.msg)

    def _raw_line(self, line: str) -> None:
        if not line.strip():
            return

        data = (line + "\n").encode(_ByteText.ENCODING)
        self._raw.feed(data)


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
        self._lines = LineSplitter()

    def feed(self, data: bytes) -> None:
        """Приём байт по мере чтения: долгий инструмент не молчит до конца."""
        for line in self._lines.feed(data):
            self._line(line)

    def flush(self) -> None:
        """Последняя строка без перевода тоже должна попасть в журнал."""
        for line in self._lines.flush():
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
        return RelaySink.level_of(name)


class ResultSink(ChannelSink, Generic[M]):
    """Ровно один JSON-конверт из tool_result; форма конверта проверяется всегда.

    Sink пути результата: его исключения фатальны для стадии. Схема `data`
    передаётся снаружи; до этапа 2 это EmptyTrailer либо сквозной JSON.
    """

    MAX_BYTES: ClassVar[int] = 1 << 20

    def __init__(self, tool: str, schema: type[M]) -> None:
        self._tool = tool
        self._schema = schema
        self._buf = bytearray()

    def feed(self, data: bytes) -> None:
        if len(self._buf) + len(data) > self.MAX_BYTES:
            msg = f"{self._tool}: tool_result exceeds {self.MAX_BYTES} bytes"
            raise LauncherError(msg)
        self._buf.extend(data)

    def close(self) -> None:
        """EOF канала; разбор конверта откладывается до итога стадии."""

    @property
    def received(self) -> bool:
        return bool(self._text())

    def failure(self) -> ResultError | None:
        """Ожидаемый отказ из конверта; None — конверт успеха."""
        envelope = self._envelope()

        if isinstance(envelope, ResultFailure):
            return envelope.error

        return None

    def success(self) -> ResultSuccess:
        envelope = self._envelope()

        if isinstance(envelope, ResultFailure):
            msg = f"{self._tool}: tool_result carries an error envelope"
            raise LauncherError(msg)

        return envelope

    def data(self) -> M:
        envelope = self.success()

        try:
            return self._schema.model_validate(envelope.data)
        except ValidationError as exc:
            msg = (
                f"{self._tool}: result data does not match "
                f"{self._schema.__name__}: {exc}"
            )
            raise LauncherError(msg) from exc

    def _text(self) -> str:
        raw = bytes(self._buf)
        return raw.decode(_ByteText.ENCODING, errors=_ByteText.ERRORS).strip()

    def _envelope(self) -> ResultSuccess | ResultFailure:
        text = self._text()

        if not text:
            msg = f"{self._tool}: tool_result is empty, one JSON envelope expected"
            raise LauncherError(msg)

        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            msg = f"{self._tool}: tool_result is not valid JSON: {exc}"
            raise LauncherError(msg) from exc

        if not isinstance(raw, dict):
            msg = f"{self._tool}: tool_result envelope must be a JSON object"
            raise LauncherError(msg)

        try:
            if "error" in raw:
                return ResultFailure.model_validate(raw)
            return ResultSuccess.model_validate(raw)
        except ValidationError as exc:
            msg = f"{self._tool}: tool_result envelope does not match contract: {exc}"
            raise LauncherError(msg) from exc


@dataclass
class _PipeEnds:
    """Пара pipe одного канала: конец родителя и конец ребёнка."""

    parent_fd: int
    child_fd: int
    parent_open: bool
    child_open: bool


class SandboxChannels:
    """Дескрипторы каналов одного запуска: pipe-пары, env и стороны ребёнка.

    Контекстный менеджер: `__exit__` закрывает все ещё открытые концы. Ошибка
    очередного `os.pipe()` не оставляет открытыми уже созданные пары.
    """

    STD_CHANNELS: ClassVar[frozenset[Channel]] = frozenset(
        (Channel.TOOL_STDIN, Channel.WRAP_STDOUT, Channel.WRAP_STDERR)
    )
    """Каналы фиксированных fd 0/1/2: едут аргументами Popen, не через env."""

    STDIN_CHILD_FD: ClassVar[int] = 0
    """Номер tool_stdin внутри песочницы: Popen ставит его в stdin ребёнка."""

    def __init__(self, channels: Iterable[Channel]) -> None:
        self._pairs: dict[Channel, _PipeEnds] = {}

        wanted = list(dict.fromkeys(channels))
        try:
            for channel in wanted:
                self._pairs[channel] = self._open_pair(channel)
        except OSError as exc:
            self._close_all()
            raise LauncherError(f"channel pipe failed: {exc}") from exc

    def __enter__(self) -> SandboxChannels:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._close_all()

    @property
    def present(self) -> tuple[Channel, ...]:
        return tuple(self._pairs)

    def has(self, channel: Channel) -> bool:
        return channel in self._pairs

    def child_fd(self, channel: Channel) -> int:
        return self._pair(channel).child_fd

    def child_fds(self) -> tuple[int, ...]:
        """Динамические концы ребёнка для pass_fds; std-каналы едут отдельно."""
        fds: list[int] = []
        for channel, pair in self._pairs.items():
            if channel in self.STD_CHANNELS:
                continue
            fds.append(pair.child_fd)

        return tuple(fds)

    def child_env(self) -> dict[str, str]:
        """Номера каналов внутрь песочницы по `Channel.env_name`.

        tool_stdin объявляется фиксированным нулевым дескриптором: сам номер
        ставит Popen, а запись в env говорит payload'у, что канал существует.
        Wrap-каналы 1/2 payload-стороне не объявляются.
        """
        env: dict[str, str] = {}
        for channel, pair in self._pairs.items():
            if channel is Channel.TOOL_STDIN:
                env[channel.env_name] = str(self.STDIN_CHILD_FD)
                continue
            if channel in self.STD_CHANNELS:
                continue
            env[channel.env_name] = str(pair.child_fd)

        return env

    def redirect_prefix(self) -> str:
        """`exec >&N 2>&M` преамбулы: fd 1/2 инструмента — tool_stdout/tool_stderr."""
        out_fd = self.child_fd(Channel.TOOL_STDOUT)
        err_fd = self.child_fd(Channel.TOOL_STDERR)

        return f"exec >&{out_fd} 2>&{err_fd}"

    def reader(self, channel: Channel) -> int:
        if channel.writes_in:
            msg = f"channel {channel} is written by the app, it has no reader"
            raise LauncherError(msg)

        return self._pair(channel).parent_fd

    def writer(self, channel: Channel) -> int:
        if not channel.writes_in:
            msg = f"channel {channel} is written by the sandbox, it has no writer"
            raise LauncherError(msg)

        return self._pair(channel).parent_fd

    def close_writer(self, channel: Channel) -> None:
        """EOF каналу внутрь: конец приложения закрывается сразу после подачи."""
        if not channel.writes_in:
            msg = f"channel {channel} is written by the sandbox, nothing to close"
            raise LauncherError(msg)

        pair = self._pair(channel)
        if not pair.parent_open:
            return

        os.close(pair.parent_fd)
        pair.parent_open = False

    def close_child_side(self) -> None:
        """Без закрытия концов ребёнка у родителя EOF по каналам не придёт."""
        for pair in self._pairs.values():
            if not pair.child_open:
                continue
            os.close(pair.child_fd)
            pair.child_open = False

    @staticmethod
    def _open_pair(channel: Channel) -> _PipeEnds:
        read_fd, write_fd = os.pipe()

        if channel.writes_in:
            pair = _PipeEnds(
                parent_fd=write_fd, child_fd=read_fd,
                parent_open=True, child_open=True,
            )
        else:
            pair = _PipeEnds(
                parent_fd=read_fd, child_fd=write_fd,
                parent_open=True, child_open=True,
            )

        os.set_inheritable(pair.child_fd, True)
        return pair

    def _pair(self, channel: Channel) -> _PipeEnds:
        pair = self._pairs.get(channel)
        if pair is None:
            raise LauncherError(f"channel {channel} is not open for this run")
        return pair

    def _close_all(self) -> None:
        for pair in self._pairs.values():
            if pair.child_open:
                os.close(pair.child_fd)
                pair.child_open = False
            if pair.parent_open:
                os.close(pair.parent_fd)
                pair.parent_open = False


@dataclass
class _PumpStage:
    """Состояние одной стадии в насосе."""

    name: str
    proc: subprocess.Popen[bytes]
    channels: SandboxChannels
    on_timeout: Callable[[], None]
    deadline: float
    timed_out: bool
    pending: int
    counters: dict[Channel, int]


@dataclass
class _ReadPort:
    """Регистрация чтения исходящего канала."""

    stage: _PumpStage
    channel: Channel
    fd: int
    sink: ChannelSink | None


@dataclass
class _WritePort:
    """Регистрация неблокирующей подачи входного канала."""

    stage: _PumpStage
    channel: Channel
    fd: int
    data: bytes
    sent: int


class ChannelPump:
    """Двунаправленный насос каналов: selector-цикл, self-pipe отмены, дедлайны.

    Контекстный менеджер: уборка (`kill` + `wait` процессов, закрытие своих
    дескрипторов и незакрытых sink'ов) выполняется при любом исходе, включая
    BaseException из sink'а. Дедлайн стадии отсчитывается от момента `add`;
    просроченная стадия убивается, каналы дочитываются до EOF, а не отдавшая
    EOF после kill (внуки держат pipe) снимается с насоса принудительно.
    """

    READ_BYTES: ClassVar[int] = 65536
    WAIT_SEC: ClassVar[float] = 5.0
    KILL_GRACE_SEC: ClassVar[float] = 5.0

    def __init__(self) -> None:
        self._selector = selectors.DefaultSelector()
        self._stages: dict[str, _PumpStage] = {}
        self._open_sinks: list[ChannelSink] = []
        self._shut = False

        wake_read, wake_write = os.pipe()
        os.set_blocking(wake_read, False)
        os.set_blocking(wake_write, False)
        self._wake_read = wake_read
        self._wake_write = wake_write
        self._wake_open = True
        self._wake_lock = threading.Lock()
        self._selector.register(wake_read, selectors.EVENT_READ, None)

    def __enter__(self) -> ChannelPump:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._shutdown()

    def add(
        self,
        stage: str,
        *,
        proc: subprocess.Popen[bytes],
        channels: SandboxChannels,
        sinks: Mapping[Channel, ChannelSink],
        feeds: Mapping[Channel, bytes],
        timeout_sec: float,
        on_timeout: Callable[[], None],
    ) -> None:
        """Регистрирует каналы стадии; момент вызова — старт её дедлайна."""
        if self._shut:
            raise LauncherError("channel pump is already shut down")

        if stage in self._stages:
            raise LauncherError(f"stage {stage!r} is already added to the pump")

        entry = _PumpStage(
            name=stage,
            proc=proc,
            channels=channels,
            on_timeout=on_timeout,
            deadline=time.monotonic() + timeout_sec,
            timed_out=False,
            pending=0,
            counters={},
        )

        for channel in channels.present:
            if channel.writes_in:
                self._add_write(entry, channel, feeds)
            else:
                self._add_read(entry, channel, sinks)

        self._stages[stage] = entry

    def run(self, cancellation: TurnCancellation) -> None:
        """Качает все каналы до EOF/дедлайнов; уборка — при любом исходе."""
        try:
            with cancellation.abort_with(self._wake):
                self._loop(cancellation)
        finally:
            self._shutdown()

    def timed_out(self, stage: str) -> bool:
        return self._stage(stage).timed_out

    def bytes_of(self, stage: str, channel: Channel) -> int:
        """Байты, прошедшие через насос по каналу стадии."""
        return self._stage(stage).counters.get(channel, 0)

    @staticmethod
    def reap(proc: subprocess.Popen[bytes]) -> None:
        """kill + wait без зомби; повторный вызов на мёртвом процессе безвреден."""
        if proc.poll() is None:
            proc.kill()

        try:
            proc.wait(timeout=ChannelPump.WAIT_SEC)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _add_write(
        self,
        entry: _PumpStage,
        channel: Channel,
        feeds: Mapping[Channel, bytes],
    ) -> None:
        data = feeds.get(channel)
        if not data:
            entry.channels.close_writer(channel)
            return

        fd = entry.channels.writer(channel)
        os.set_blocking(fd, False)

        port = _WritePort(stage=entry, channel=channel, fd=fd, data=data, sent=0)
        self._selector.register(fd, selectors.EVENT_WRITE, port)
        entry.pending += 1

    def _add_read(
        self,
        entry: _PumpStage,
        channel: Channel,
        sinks: Mapping[Channel, ChannelSink],
    ) -> None:
        fd = entry.channels.reader(channel)
        os.set_blocking(fd, False)

        sink = sinks.get(channel)
        if sink is not None:
            self._open_sinks.append(sink)

        entry.counters[channel] = 0
        port = _ReadPort(stage=entry, channel=channel, fd=fd, sink=sink)
        self._selector.register(fd, selectors.EVENT_READ, port)
        entry.pending += 1

    def _loop(self, cancellation: TurnCancellation) -> None:
        while self._alive():
            if cancellation.cancelled:
                # мёртвая стадия могла оставить каналы открытыми у внуков:
                # EOF не ждём, уборка добьёт процессы
                return

            events = self._selector.select(self._poll_sec())
            self._fire_deadlines()

            for key, _mask in events:
                self._dispatch(key)

    def _alive(self) -> bool:
        for entry in self._stages.values():
            if entry.pending > 0:
                return True
        return False

    def _poll_sec(self) -> float:
        deadlines: list[float] = []
        for entry in self._stages.values():
            if entry.pending > 0:
                deadlines.append(entry.deadline)

        if not deadlines:
            return 0.0

        wait = min(deadlines) - time.monotonic()
        if wait < 0.0:
            return 0.0

        return wait

    def _fire_deadlines(self) -> None:
        now = time.monotonic()

        for entry in self._stages.values():
            if entry.pending <= 0:
                continue
            if now < entry.deadline:
                continue
            if not entry.timed_out:
                entry.timed_out = True
                entry.deadline = now + self.KILL_GRACE_SEC
                entry.on_timeout()
                continue
            self._abandon(entry)

    def _abandon(self, entry: _PumpStage) -> None:
        """Каналы не дали EOF после kill: стадия снимается с насоса принудительно."""
        for key in list(self._selector.get_map().values()):
            port = key.data
            if port is None:
                continue
            if port.stage is not entry:
                continue

            self._selector.unregister(key.fd)
            if isinstance(port, _WritePort):
                entry.channels.close_writer(port.channel)
                continue
            self._close_sink(port.sink)

        entry.pending = 0

    def _dispatch(self, key: selectors.SelectorKey) -> None:
        port = key.data

        if port is None:
            self._drain_wake()
            return

        # порт мог быть снят _abandon'ом внутри этой же пачки событий
        if key.fd not in self._selector.get_map():
            return

        if isinstance(port, _ReadPort):
            self._pump_read(port)
            return

        self._pump_write(port)

    def _drain_wake(self) -> None:
        try:
            while os.read(self._wake_read, 4096):
                pass
        except BlockingIOError:
            return

    def _pump_read(self, port: _ReadPort) -> None:
        try:
            chunk = os.read(port.fd, self.READ_BYTES)
        except BlockingIOError:
            return
        except OSError as exc:
            msg = f"channel {port.channel} read failed: {exc}"
            raise LauncherError(msg) from exc

        if not chunk:
            self._finish_read(port)
            return

        port.stage.counters[port.channel] += len(chunk)

        if port.sink is None:
            return

        port.sink.feed(chunk)

    def _pump_write(self, port: _WritePort) -> None:
        remainder = memoryview(port.data)[port.sent :]

        try:
            sent = os.write(port.fd, remainder)
        except BlockingIOError:
            return
        except BrokenPipeError:
            # ребёнок умер, не дочитав канал: исход объяснит его код возврата
            self._finish_write(port)
            return
        except OSError as exc:
            msg = f"channel {port.channel} write failed: {exc}"
            raise LauncherError(msg) from exc

        port.sent += sent
        if port.sent >= len(port.data):
            self._finish_write(port)

    def _finish_read(self, port: _ReadPort) -> None:
        self._selector.unregister(port.fd)
        port.stage.pending -= 1
        self._close_sink(port.sink)

    def _finish_write(self, port: _WritePort) -> None:
        self._selector.unregister(port.fd)
        port.stage.pending -= 1
        port.stage.channels.close_writer(port.channel)

    def _close_sink(self, sink: ChannelSink | None) -> None:
        if sink is None:
            return
        if sink not in self._open_sinks:
            return

        self._open_sinks.remove(sink)
        sink.close()

    def _wake(self) -> None:
        """Прерыватель отмены: байт в self-pipe будит selector из чужого потока.

        Лок с уборкой обязателен: без него закрытый номер дескриптора могла бы
        занять чужая нить, и запись попала бы в посторонний файл.
        """
        with self._wake_lock:
            if not self._wake_open:
                return

            try:
                os.write(self._wake_write, b"x")
            except OSError:
                # pipe переполнен: насос и так проснётся
                return

    def _stage(self, stage: str) -> _PumpStage:
        entry = self._stages.get(stage)
        if entry is None:
            raise LauncherError(f"unknown stage {stage!r}")
        return entry

    def _shutdown(self) -> None:
        """Уборка при любом исходе: без зомби, без открытых своих fd и sink'ов."""
        if self._shut:
            return
        self._shut = True

        for entry in self._stages.values():
            self.reap(entry.proc)

        for sink in list(self._open_sinks):
            self._open_sinks.remove(sink)
            try:
                sink.close()
            except Exception:
                logger.exception("channel sink close failed during pump shutdown")

        with self._wake_lock:
            self._wake_open = False
            os.close(self._wake_write)

        self._selector.close()
        os.close(self._wake_read)


@dataclass(frozen=True)
class StageRunResult(Generic[M]):
    """Итог одиночной канальной стадии: трейлер, счётчик данных, процессные факты."""

    tool: str
    trailer: M
    bytes_out: int
    duration_ms: int
    diagnostic: str


@dataclass(frozen=True)
class _ImageChain:
    """Разложение профиля с образами под fuse-цепочку лаунчера."""

    inner: SandboxProfile
    images: tuple[tuple[str, str], ...]
    rw_paths: tuple[str, ...]


@dataclass(frozen=True)
class _StageLaunch:
    """Собранный канальный запуск: argv, подача wrap-каналов и способ лимитов.

    wrap_feeds — байты профилей по каналам: у прямой стадии один wrap_args,
    у стадии с образами wrap_args (опции внешнего bwrap) плюс wrap_args_inner
    (профиль вложенного). runner_limits None — rlimit'ы команде ставит лаунчер
    цепочки, обвязку prlimit'ом не душим.
    """

    argv: tuple[str, ...]
    wrap_feeds: Mapping[Channel, bytes]
    runner_limits: ResourceLimits | None


class ShellRunnerInvariantError(Exception):
    """Нарушен инвариант runner'а (в отличие от assert переживает -O)."""


class ShellRunner:
    """Subprocess-обёртка старого кадрового пути: argv + timeout + size-cap.

    Умирает на этапе 3 вместе с кадровым протоколом; канальный путь качает
    ChannelPump.
    """

    READ_BYTES: ClassVar[int] = 65536

    @classmethod
    def cgroup_preexec(cls, cgroup_dir: str | None) -> Callable[[], None] | None:
        """Вход в cgroup до exec: всё дерево рождается уже внутри leaf'а."""
        if cgroup_dir is None:
            return None

        procs_path = os.path.join(cgroup_dir, "cgroup.procs")

        def enter_cgroup() -> None:
            fd = os.open(procs_path, os.O_WRONLY)
            os.write(fd, b"0")
            os.close(fd)

        return enter_cgroup

    @classmethod
    def run(  # noqa: PLR0913
        cls,
        argv: list[str],
        *,
        stdin_data: bytes,
        timeout_sec: int,
        max_output_bytes: int,
        cwd: str,
        env: Mapping[str, str],
        stdout_sink: Callable[[bytes], None] | None,
        keep_stdout: bool,
        stderr_sink: Callable[[bytes], None] | None = None,
        limits: ResourceLimits | None = None,
        cgroup_dir: str | None = None,
    ) -> RunResult:
        if not argv:
            raise ValueError("run_subprocess: argv must not be empty")
        if not cwd:
            raise ValueError("run_subprocess: cwd must be a non-empty string")

        preexec = cls.cgroup_preexec(cgroup_dir)

        started = time.monotonic()
        proc = subprocess.Popen(  # noqa: S603
            argv,
            shell=False,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            close_fds=True,
            cwd=cwd,
            env=dict(env),
            preexec_fn=preexec,  # noqa: PLW1509
        )
        if limits is not None:
            limits.apply_to_process(proc.pid)
        cancellation = current_cancellation()
        with cancellation.abort_with(proc.kill):
            cls._feed_stdin(proc, stdin_data)
            out_bytes, err_bytes, trunc_out, trunc_err, timed_out = cls._pump(
                proc, timeout_sec, max_output_bytes, cancellation,
                stdout_sink, stderr_sink, keep_stdout,
            )
        cancellation.raise_if_cancelled()
        duration_ms = int((time.monotonic() - started) * 1000)

        exit_code = proc.returncode
        if exit_code is None:
            exit_code = -9

        return RunResult(
            exit_code=exit_code,
            stdout=out_bytes.decode(_ByteText.ENCODING, errors=_ByteText.ERRORS),
            stderr=err_bytes.decode(_ByteText.ENCODING, errors=_ByteText.ERRORS),
            truncated_stdout=trunc_out,
            truncated_stderr=trunc_err,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )

    @staticmethod
    def _feed_stdin(proc: subprocess.Popen[bytes], data: bytes) -> None:
        if proc.stdin is None:
            raise ShellRunnerInvariantError(
                "_feed_stdin: proc.stdin expected (Popen started with PIPE)",
            )
        try:
            if data:
                proc.stdin.write(data)
        except BrokenPipeError:
            pass
        finally:
            proc.stdin.close()

    @classmethod
    def _pump(  # noqa: PLR0913
        cls,
        proc: subprocess.Popen[bytes],
        timeout_sec: int,
        max_output_bytes: int,
        cancellation: TurnCancellation,
        stdout_sink: Callable[[bytes], None] | None,
        stderr_sink: Callable[[bytes], None] | None,
        keep_stdout: bool,
    ) -> tuple[bytes, bytes, bool, bool, bool]:
        if proc.stdout is None or proc.stderr is None:
            raise ShellRunnerInvariantError(
                "_pump: proc.stdout and proc.stderr expected (Popen started with PIPE)"
            )

        deadline = time.monotonic() + timeout_sec
        fds = {proc.stdout.fileno(): "out", proc.stderr.fileno(): "err"}
        buffers = {"out": bytearray(), "err": bytearray()}
        sinks: dict[str, Callable[[bytes], None] | None] = {
            "out": stdout_sink,
            "err": stderr_sink,
        }
        # stderr копится и при живом релее: его хвост объясняет падение процесса
        keeps = {"out": keep_stdout, "err": True}
        truncated = {"out": False, "err": False}
        open_fds = set(fds.keys())

        try:
            timed_out = cls._select_loop(
                deadline, fds, buffers, sinks, keeps, truncated, open_fds,
                max_output_bytes, cancellation,
            )
        except Exception:
            # потребитель оборвал поток: процесс дальше не нужен
            proc.kill()
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()
            raise

        if timed_out:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        proc.stdout.close()
        proc.stderr.close()
        return (
            bytes(buffers["out"]),
            bytes(buffers["err"]),
            truncated["out"],
            truncated["err"],
            timed_out,
        )

    @classmethod
    def _select_loop(  # noqa: PLR0913
        cls,
        deadline: float,
        fds: dict[int, str],
        buffers: dict[str, bytearray],
        sinks: dict[str, Callable[[bytes], None] | None],
        keeps: dict[str, bool],
        truncated: dict[str, bool],
        open_fds: set[int],
        max_output_bytes: int,
        cancellation: TurnCancellation,
    ) -> bool:
        """Чтение обоих потоков до EOF; True — упёрлись в дедлайн."""
        while open_fds:
            if cancellation.cancelled:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            ready, _, _ = select.select(list(open_fds), [], [], min(remaining, 1.0))
            if not ready:
                continue
            for fd in ready:
                tag = fds[fd]
                more = cls._read_chunk(
                    fd, buffers[tag], truncated, tag,
                    max_output_bytes, sinks[tag], keeps[tag],
                )
                if not more:
                    open_fds.discard(fd)
        return False

    @classmethod
    def _read_chunk(  # noqa: PLR0913
        cls,
        fd: int,
        buf: bytearray,
        truncated: dict[str, bool],
        tag: str,
        max_output_bytes: int,
        sink: Callable[[bytes], None] | None,
        keep: bool,
    ) -> bool:
        chunk = os.read(fd, cls.READ_BYTES)
        if not chunk:
            return False
        if sink is not None:
            sink(chunk)
        if not keep:
            return True
        if truncated[tag]:
            return True
        room = max_output_bytes - len(buf)
        if len(chunk) <= room:
            buf.extend(chunk)
        else:
            if room > 0:
                buf.extend(chunk[:room])
            truncated[tag] = True
        return True


class SandboxRunner:
    """Выполняет команду в уже собранном профиле песочницы."""

    FAIL_TAIL_CHARS: ClassVar[int] = 2000

    TAIL_BYTES: ClassVar[int] = 65536
    """Потолок хвостов stderr/wrap-каналов для диагностики канального пути."""

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
            result = ShellRunner.run(
                argv,
                stdin_data=stdin.encode(_ByteText.ENCODING),
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

    def run_stage(
        self,
        command: str,
        *,
        args: str,
        stdin: bytes,
        stdout: ChannelSink,
        payload: ChannelSink | None,
        schema: type[M],
    ) -> StageRunResult[M]:
        """Одиночная стадия по канальному контракту: без графа и mount-групп.

        `args` — JSON запроса в `tool_args`; `stdin` — литеральный вход;
        `payload=None` — канал `tool_payload` не создаётся (`out = None`).
        Ожидаемый отказ из конверта — PayloadFailureError, всё остальное
        нарушение — LauncherError.
        """
        name = self._label()

        try:
            rendered = self._profile.render(dict(self._path_vars()))
            self._prepare_dirs(rendered)
        except Exception as exc:
            raise LauncherError(f"{name}: profile is not runnable: {exc}") from exc

        limits = self.limits_of(rendered)
        group = GroupLimits.of_profile(rendered)
        cancellation = current_cancellation()

        wanted: list[Channel] = [
            Channel.WRAP_ARGS,
            Channel.TOOL_ARGS,
            Channel.TOOL_STDIN,
            Channel.TOOL_STDOUT,
            Channel.TOOL_STDERR,
            Channel.TOOL_RESULT,
            Channel.WRAP_STDOUT,
            Channel.WRAP_STDERR,
        ]
        # у стадии с образами профиль вложенного bwrap едет каналом wrap_args_inner
        if rendered.rw_images:
            wanted.append(Channel.WRAP_ARGS_INNER)
        if payload is not None:
            wanted.append(Channel.TOOL_PAYLOAD)

        stderr_tail = TailSink(self.TAIL_BYTES)
        wrap_out_tail = TailSink(self.TAIL_BYTES)
        wrap_err_tail = TailSink(self.TAIL_BYTES)
        result_sink: ResultSink[M] = ResultSink(name, schema)

        sinks: dict[Channel, ChannelSink] = {
            Channel.TOOL_STDOUT: stdout,
            Channel.TOOL_STDERR: RelaySink(name, raw=stderr_tail),
            Channel.TOOL_RESULT: result_sink,
            Channel.WRAP_STDOUT: wrap_out_tail,
            Channel.WRAP_STDERR: wrap_err_tail,
        }
        if payload is not None:
            sinks[Channel.TOOL_PAYLOAD] = payload

        self._log_start(name, rendered, limits, command)
        started = time.monotonic()

        with ExitStack() as stack:
            channels = stack.enter_context(SandboxChannels(wanted))

            env = self._env_of(rendered) | channels.child_env()
            launch = self._stage_launch(rendered, command, env, channels, limits)

            cgroup_dir: str | None = None
            if group.requested:
                manager = CgroupManager(rendered.cgroup_base)
                try:
                    cgroup_dir = manager.acquire(group)
                except CgroupError as exc:
                    msg = f"{name}: cgroup acquire failed: {exc}"
                    raise LauncherError(msg) from exc
                stack.callback(manager.release, cgroup_dir)
                logger.info(
                    "sandbox[%s]: cgroup %s (%s)", name, cgroup_dir, group.describe()
                )

            proc = self._spawn(launch.argv, channels, cgroup_dir)
            # процесс захвачен до регистрации прерывателя: ранний ToolStopped
            # разматывает стек, а не теряет процесс
            stack.callback(ChannelPump.reap, proc)
            stack.enter_context(cancellation.abort_with(proc.kill))

            channels.close_child_side()

            if launch.runner_limits is not None:
                try:
                    launch.runner_limits.apply_to_process(proc.pid)
                except OSError as exc:
                    msg = f"{name}: rlimit setup failed: {exc}"
                    raise LauncherError(msg) from exc

            feeds: dict[Channel, bytes] = dict(launch.wrap_feeds)
            feeds[Channel.TOOL_ARGS] = args.encode(_ByteText.ENCODING)
            feeds[Channel.TOOL_STDIN] = stdin

            pump = stack.enter_context(ChannelPump())
            pump.add(
                name,
                proc=proc,
                channels=channels,
                sinks=sinks,
                feeds=feeds,
                timeout_sec=rendered.timeout_sec,
                on_timeout=proc.kill,
            )
            pump.run(cancellation)

            timed_out = pump.timed_out(name)
            moved = pump.bytes_of(name, Channel.TOOL_PAYLOAD)
            exit_code = ShellExit.of(proc.returncode)

        cancellation.raise_if_cancelled()

        duration_ms = int((time.monotonic() - started) * 1000)
        stderr_text = self._merge_tails(stderr_tail, wrap_err_tail)
        truncated = stderr_tail.truncated or wrap_err_tail.truncated
        facts = RunResult(
            exit_code=exit_code,
            stdout="",
            stderr=stderr_text,
            truncated_stdout=False,
            truncated_stderr=truncated,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )

        diagnostic = SandboxDiagnostics.explain(facts, rendered)
        if diagnostic:
            logger.warning("sandbox[%s]: %s", name, diagnostic)

        self._log_finish(name, facts)
        if timed_out or exit_code != 0:
            self._log_failure(name, facts)

        context = self._stage_context(stderr_text, diagnostic)

        if timed_out:
            raise LauncherError(f"{name}: timed out; {context}")

        # конверт ошибки объясняет отказ сам: он важнее ненулевого кода возврата
        failure = self._declared_failure(result_sink, exit_code)
        if failure is not None:
            message = failure.message
            if diagnostic:
                message = f"{message}; {diagnostic}"
            raise PayloadFailureError(failure.kind, message)

        if exit_code != 0:
            msg = f"{name}: exited with code {exit_code}; {context}"
            raise LauncherError(msg)

        if not result_sink.received:
            msg = f"{name}: no envelope in tool_result; {context}"
            raise LauncherError(msg)

        envelope = result_sink.success()
        trailer = result_sink.data()

        if envelope.bytes_out != moved:
            msg = (
                f"{name}: bytes_out mismatch: envelope says {envelope.bytes_out}, "
                f"pump moved {moved}"
            )
            raise LauncherError(msg)

        return StageRunResult(
            tool=name,
            trailer=trailer,
            bytes_out=envelope.bytes_out,
            duration_ms=duration_ms,
            diagnostic=diagnostic,
        )

    def _stage_launch(
        self,
        rendered: SandboxProfile,
        command: str,
        env: Mapping[str, str],
        channels: SandboxChannels,
        limits: ResourceLimits,
    ) -> _StageLaunch:
        """Сборка argv стадии: без образов — прямой bwrap, с образами — fuse-цепочка."""
        if not rendered.rw_images:
            built = ChannelArgv.build(
                rendered,
                command,
                env=env,
                wrap_args_fd=channels.child_fd(Channel.WRAP_ARGS),
                redirect_prefix=channels.redirect_prefix(),
            )

            return _StageLaunch(
                argv=built.argv,
                wrap_feeds={Channel.WRAP_ARGS: built.wrap_args},
                runner_limits=limits,
            )

        chain = self._image_chain(rendered)

        built = ChannelArgv.build(
            chain.inner,
            command,
            env=env,
            wrap_args_fd=channels.child_fd(Channel.WRAP_ARGS_INNER),
            redirect_prefix=channels.redirect_prefix(),
            nested=True,
        )

        channel_env = channels.child_env()
        # wrap_args вычитывает --args внешнего bwrap: вниз по цепочке не передаётся
        channel_env.pop(Channel.WRAP_ARGS.env_name, None)

        try:
            require_fuse()
            chain_argv = ChainChannelArgv.build(
                images=chain.images,
                template=rendered.image_template,
                op=[LauncherMode.RUN.value, shlex.join(built.argv)],
                python_bin=sys.executable,
                options=rendered.launcher.to_options(),
                limits=limits,
                wrap_args_fd=channels.child_fd(Channel.WRAP_ARGS),
                rw_paths=chain.rw_paths,
                network=rendered.network,
                channel_env=channel_env,
            )
        except MountError as exc:
            raise LauncherError(f"image chain is not runnable: {exc}") from exc

        wrap_feeds = {
            Channel.WRAP_ARGS: WrapArgsCodec.encode(chain_argv.bwrap_options),
            Channel.WRAP_ARGS_INNER: built.wrap_args,
        }

        return _StageLaunch(
            argv=chain_argv.argv,
            wrap_feeds=wrap_feeds,
            runner_limits=None,
        )

    @staticmethod
    def _image_chain(profile: SandboxProfile) -> _ImageChain:
        """Пары (образ, mountpoint), rw-пути и inner-профиль со смонтированными точками."""
        mounts: list[BindSpec] = []
        images: list[tuple[str, str]] = []
        for spec in profile.rw_images:
            mnt = f"{spec.host}.mnt"
            images.append((spec.host, mnt))
            mounts.append(BindSpec(host=mnt, target=spec.target))

        rw_paths: list[str] = []
        for spec in profile.rw_binds:
            rw_paths.append(spec.host)

        inner = profile.model_copy(
            update={"rw_binds": profile.rw_binds + tuple(mounts)},
        )

        return _ImageChain(
            inner=inner,
            images=tuple(images),
            rw_paths=tuple(rw_paths),
        )

    def _label(self) -> str:
        """Профиль плюс имя инструмента: sandbox[confluence:confluence_search]."""
        call = ToolCallContext.get()
        if call and call != self._tool:
            return f"{self._tool}:{call}"
        return self._tool

    def _spawn(
        self,
        argv: Sequence[str],
        channels: SandboxChannels,
        cgroup_dir: str | None,
    ) -> subprocess.Popen[bytes]:
        preexec = ShellRunner.cgroup_preexec(cgroup_dir)

        try:
            return subprocess.Popen(  # noqa: S603
                list(argv),
                shell=False,
                stdin=channels.child_fd(Channel.TOOL_STDIN),
                stdout=channels.child_fd(Channel.WRAP_STDOUT),
                stderr=channels.child_fd(Channel.WRAP_STDERR),
                bufsize=0,
                close_fds=True,
                pass_fds=channels.child_fds(),
                cwd="/",
                env=dict(os.environ),
                preexec_fn=preexec,  # noqa: PLW1509
            )
        except OSError as exc:
            raise LauncherError(f"sandbox spawn failed: {exc}") from exc

    @staticmethod
    def _declared_failure(
        sink: ResultSink[M],
        exit_code: int,
    ) -> ResultError | None:
        """Ожидаемый отказ из конверта; битый конверт упавшего процесса — не отказ."""
        if not sink.received:
            return None

        try:
            return sink.failure()
        except LauncherError:
            if exit_code == 0:
                raise
            # процесс упал посреди конверта: причину объяснят rc и stderr
            return None

    @staticmethod
    def _merge_tails(*tails: TailSink) -> str:
        parts: list[str] = []
        for tail in tails:
            text = tail.text().strip()
            if text:
                parts.append(text)

        return "\n".join(parts)

    @classmethod
    def _stage_context(cls, stderr_text: str, diagnostic: str) -> str:
        tail = cls._tail(stderr_text)
        parts = [f"stderr={tail!r}"]
        if diagnostic:
            parts.append(diagnostic)

        return "; ".join(parts)

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
        chain = self._image_chain(profile)
        inner_argv = build_bwrap_argv(chain.inner, command, env=env, nested=True)
        argv = build_chain_argv(
            images=chain.images,
            template=profile.image_template,
            op=[LauncherMode.RUN.value, shlex.join(inner_argv)],
            python_bin=sys.executable,
            options=profile.launcher.to_options(),
            limits=limits,
            rw_paths=chain.rw_paths,
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
