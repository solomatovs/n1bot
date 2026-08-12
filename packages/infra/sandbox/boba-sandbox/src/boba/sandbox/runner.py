"""Каналы одного запуска, насос, приёмники байтов и контекст вызова.

Исполнение живёт в workflow.py, журнал — в journal.py; контекст вызова
(адресат журнала) стоит здесь, потому что его ставит обвязка инструмента, а
не исполнитель графа.

Ошибки: LauncherError и наследники (PayloadFailureError); исключения sink'ов
пути данных и результата проходят наверх как есть — их контракт задаёт
потребитель потока.
"""

from __future__ import annotations

import json
import logging
import os
import selectors
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import ClassVar, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from boba.cancellation import TurnCancellation
from boba.sandbox.profile import SandboxProfile
from boba.toolkit.binaries import SandboxBinary
from boba.toolkit.channels import (
    ByteText,
    Channel,
    ChannelError,
    ChannelSink,
    LineSplitter,
    LogFrame,
    ResultError,
    ResultFailure,
    ResultSuccess,
    SafeSegment,
    ShellExit,
    StageExit,
    StreamKey,
)
from boba.toolkit.launcher import LauncherError


def has_bwrap(profile: SandboxProfile) -> bool:
    """Есть ли bubblewrap в доверенных каталогах: без него песочницу не поднять."""
    return profile.binaries.has(SandboxBinary.BWRAP)


M = TypeVar("M", bound=BaseModel)

__all__ = [
    "ChannelPump",
    "ChannelSink",
    "FanoutSink",
    "KillCause",
    "LineSplitter",
    "PipeSink",
    "RelaySink",
    "ResultSink",
    "SandboxChannels",
    "TailSink",
    "ToolCallContext",
    "WrapResultSink",
    "has_bwrap",
]

logger = logging.getLogger(__name__)


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
        return bytes(self._buf).decode(ByteText.ENCODING, errors=ByteText.ERRORS)


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

        data = (line + "\n").encode(ByteText.ENCODING)
        self._raw.feed(data)


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
        return raw.decode(ByteText.ENCODING, errors=ByteText.ERRORS).strip()

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


class WrapResultSink(ChannelSink):
    """Итоги стадий mount-группы из wrap_result: JSON-строки `{stage, rc}`.

    Sink пути результата: переполнение фатально для запуска, разбор строк
    откладывается до опроса итога — как у ResultSink.
    """

    MAX_BYTES: ClassVar[int] = 1 << 20

    def __init__(self, group: str) -> None:
        self._group = group
        self._lines = LineSplitter()
        self._received: list[str] = []
        self._size = 0

    def feed(self, data: bytes) -> None:
        self._size += len(data)
        if self._size > self.MAX_BYTES:
            msg = f"{self._group}: wrap_result exceeds {self.MAX_BYTES} bytes"
            raise LauncherError(msg)

        for line in self._lines.feed(data):
            self._keep(line)

    def close(self) -> None:
        for line in self._lines.flush():
            self._keep(line)

    def exits(self) -> dict[str, int]:
        """Коды стадий по строкам канала; битая строка или дубль — LauncherError."""
        exits: dict[str, int] = {}
        for line in self._received:
            entry = self._parse(line)
            if entry.stage in exits:
                msg = f"{self._group}: duplicate wrap_result for {entry.stage!r}"
                raise LauncherError(msg)
            exits[entry.stage] = entry.rc

        return exits

    def rc_of(self, stage: str) -> int:
        exits = self.exits()

        if stage not in exits:
            msg = f"{self._group}: no wrap_result line for stage {stage!r}"
            raise LauncherError(msg)

        return exits[stage]

    def _keep(self, line: str) -> None:
        if not line.strip():
            return

        self._received.append(line)

    def _parse(self, line: str) -> StageExit:
        try:
            return StageExit.decode_line(line)
        except ChannelError as exc:
            raise LauncherError(f"{self._group}: {exc}") from exc


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

    def close_reader(self, channel: Channel) -> None:
        """Чтение канала закрывается до EOF: следующая запись источника — EPIPE."""
        if channel.writes_in:
            msg = f"channel {channel} is written by the app, it has no reader"
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
                parent_fd=write_fd,
                child_fd=read_fd,
                parent_open=True,
                child_open=True,
            )
        else:
            pair = _PipeEnds(
                parent_fd=read_fd,
                child_fd=write_fd,
                parent_open=True,
                child_open=True,
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


class KillCause(StrEnum):
    """Причина убийства стадии насосом."""

    NONE = "none"
    TIMEOUT = "timeout"
    CASCADE = "cascade"


@dataclass
class _PumpStage:
    """Состояние одной стадии в насосе."""

    name: str
    proc: subprocess.Popen[bytes]
    channels: SandboxChannels
    on_timeout: Callable[[], None]
    started: float
    ended: float | None
    deadline: float | None
    """None — стадия без таймаута; после kill сюда встаёт срок добивания."""
    kill_cause: KillCause
    exit_seen: bool
    edges: tuple[PipeSink, ...]
    pending: int
    counters: dict[Channel, int]


@dataclass
class _ReadPort:
    """Регистрация чтения исходящего канала."""

    stage: _PumpStage
    channel: Channel
    fd: int
    sink: ChannelSink | None
    paused: bool


@dataclass
class _WritePort:
    """Регистрация неблокирующей подачи входного канала."""

    stage: _PumpStage
    channel: Channel
    fd: int
    data: bytes
    sent: int


@dataclass
class _EdgeState:
    """Состояние write-конца ребра: ограниченный буфер и судьба доставки."""

    name: str
    fd: int
    limit: int
    buf: bytearray
    delivered: int
    fd_open: bool
    registered: bool
    eof: bool
    closed_early: bool


class ChannelPump:
    """Двунаправленный насос каналов: selector-цикл, self-pipe отмены, дедлайны.

    Контекстный менеджер: уборка (`kill` + `wait` процессов, закрытие своих
    дескрипторов, рёбер и незакрытых sink'ов) выполняется при любом исходе,
    включая BaseException из sink'а. Дедлайн стадии отсчитывается от момента
    `add`; просроченная стадия убивается, каналы дочитываются до EOF, а не
    отдавшая EOF после kill (внуки держат pipe) снимается принудительно.

    Рёбра графа (`add_edge`) — write-концы pipe'ов потребителей: заполненный
    буфер ребра приостанавливает чтение источника (backpressure), EOF
    источника закрывает write-конец после долива буфера, а когда все рёбра
    источника закрылись потребителями — насос закрывает чтение его канала и
    следующая запись источника получает EPIPE. Стадия с ненулевым кодом
    возврата валит остальные каскадом; причины убийств (`kill_cause`) и
    признаки рёбер (`PipeSink.broken`) насос отдаёт вместе с итогами.

    Исключение sink'а пути данных или результата фатально: процессы убиваются
    уборкой, ошибка идёт наверх; побочные sink'и (релей, журнал) глушат свои
    ошибки сами и поток стадии не рвут.
    """

    READ_BYTES: ClassVar[int] = 65536
    WAIT_SEC: ClassVar[float] = 5.0
    KILL_GRACE_SEC: ClassVar[float] = 5.0
    EXIT_POLL_SEC: ClassVar[float] = 0.05
    EDGE_BUFFER_BYTES: ClassVar[int] = 1 << 20

    def __init__(self) -> None:
        self._selector = selectors.DefaultSelector()
        self._stages: dict[str, _PumpStage] = {}
        self._edges: dict[str, _EdgeState] = {}
        self._paused: list[_ReadPort] = []
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

    def add(  # noqa: PLR0913
        self,
        stage: str,
        *,
        proc: subprocess.Popen[bytes],
        channels: SandboxChannels,
        sinks: Mapping[Channel, ChannelSink],
        feeds: Mapping[Channel, bytes],
        timeout_sec: float | None,
        on_timeout: Callable[[], None],
    ) -> None:
        """Регистрирует каналы стадии; момент вызова — старт её дедлайна.

        `timeout_sec` None — профиль стадии не ограничивает её по времени;
        дедлайн появляется только на добивание после kill.

        `on_timeout` — рубильник стадии: насос зовёт его и по дедлайну, и при
        каскаде от сбоя соседней стадии.
        """
        if self._shut:
            raise LauncherError("channel pump is already shut down")

        if stage in self._stages:
            raise LauncherError(f"stage {stage!r} is already added to the pump")

        now = time.monotonic()

        deadline: float | None = None
        if timeout_sec is not None:
            deadline = now + timeout_sec

        entry = _PumpStage(
            name=stage,
            proc=proc,
            channels=channels,
            on_timeout=on_timeout,
            started=now,
            ended=None,
            deadline=deadline,
            kill_cause=KillCause.NONE,
            exit_seen=False,
            edges=self._stage_edges(sinks),
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

    def add_edge(self, edge: str, write_fd: int, *, buffer_limit: int) -> PipeSink:
        """Регистрирует ребро: насос владеет write-концом и закрывает его сам.

        `buffer_limit` — потолок недописанных байт ребра; заполненный буфер
        приостанавливает чтение источника до долива (backpressure).
        """
        if self._shut:
            raise LauncherError("channel pump is already shut down")

        if edge in self._edges:
            raise LauncherError(f"edge {edge!r} is already added to the pump")

        if buffer_limit <= 0:
            raise LauncherError(f"edge {edge!r}: buffer_limit must be positive")

        os.set_blocking(write_fd, False)

        state = _EdgeState(
            name=edge,
            fd=write_fd,
            limit=buffer_limit,
            buf=bytearray(),
            delivered=0,
            fd_open=True,
            registered=False,
            eof=False,
            closed_early=False,
        )
        self._edges[edge] = state

        return PipeSink(self, state)

    def kill_cause(self, stage: str) -> KillCause:
        """Кем убита стадия: дедлайном, каскадом либо не убита вовсе."""
        return self._stage(stage).kill_cause

    def timed_out(self, stage: str) -> bool:
        return self._stage(stage).kill_cause is KillCause.TIMEOUT

    def bytes_of(self, stage: str, channel: Channel) -> int:
        """Байты, прошедшие через насос по каналу стадии."""
        return self._stage(stage).counters.get(channel, 0)

    def duration_ms(self, stage: str) -> int:
        """Длительность стадии: от регистрации до увиденного завершения."""
        entry = self._stage(stage)

        ended = entry.ended
        if ended is None:
            ended = time.monotonic()

        return max(0, int((ended - entry.started) * 1000))

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
        port = _ReadPort(stage=entry, channel=channel, fd=fd, sink=sink, paused=False)
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

            self._reap_finished()

    def _alive(self) -> bool:
        pending = False
        for entry in self._stages.values():
            if entry.pending > 0:
                pending = True

        return pending

    def _poll_sec(self) -> float | None:
        """None — ждать событий бессрочно: ни одна живая стадия не ограничена."""
        deadlines: list[float] = []
        for entry in self._stages.values():
            if entry.pending <= 0:
                continue
            if entry.deadline is None:
                continue

            deadlines.append(entry.deadline)

        if not deadlines:
            if self._exit_pending():
                return self.EXIT_POLL_SEC

            return None

        wait = min(deadlines) - time.monotonic()
        wait = max(wait, 0.0)

        if self._exit_pending():
            wait = min(wait, self.EXIT_POLL_SEC)

        return wait

    def _exit_pending(self) -> bool:
        """Есть стадия с EOF по каналам, но не увиденным кодом возврата."""
        for entry in self._stages.values():
            if entry.exit_seen:
                continue
            if entry.pending <= 0:
                return True

        return False

    def _fire_deadlines(self) -> None:
        now = time.monotonic()

        for entry in self._stages.values():
            if entry.pending <= 0:
                continue
            if entry.deadline is None:
                continue
            if now < entry.deadline:
                continue
            if entry.kill_cause is KillCause.NONE:
                entry.kill_cause = KillCause.TIMEOUT
                entry.deadline = now + self.KILL_GRACE_SEC
                entry.on_timeout()
                continue
            self._abandon(entry)

    def _reap_finished(self) -> None:
        """Опрос кодов возврата: сбойная стадия каскадом валит остальные."""
        for entry in list(self._stages.values()):
            if entry.exit_seen:
                continue

            self._poll_exit(entry)
            if not entry.exit_seen:
                continue

            code = ShellExit.of(entry.proc.returncode)
            if code == 0:
                continue

            if entry.kill_cause is KillCause.CASCADE:
                continue

            benign_sigpipe = False
            if code == ShellExit.SIGPIPE:
                benign_sigpipe = self._broken_edge(entry)

            if benign_sigpipe:
                # обрыв потребителем: судьбу источника решает раннер (rc 141)
                continue

            self._cascade(entry)

    @staticmethod
    def _poll_exit(entry: _PumpStage) -> None:
        if entry.proc.poll() is None:
            return

        entry.exit_seen = True

        if entry.ended is None:
            entry.ended = time.monotonic()

    @staticmethod
    def _broken_edge(entry: _PumpStage) -> bool:
        broken = False
        for pipe in entry.edges:
            if pipe.broken:
                broken = True

        return broken

    def _cascade(self, origin: _PumpStage) -> None:
        """Убить живые стадии: их 137 — SIGKILL насоса, не собственный сбой."""
        now = time.monotonic()

        for entry in self._stages.values():
            if entry is origin:
                continue
            if entry.kill_cause is not KillCause.NONE:
                continue

            if not entry.exit_seen:
                self._poll_exit(entry)
            if entry.exit_seen:
                continue

            entry.kill_cause = KillCause.CASCADE
            entry.deadline = now + self.KILL_GRACE_SEC
            entry.on_timeout()

    def _abandon(self, entry: _PumpStage) -> None:
        """Каналы не дали EOF после kill: стадия снимается с насоса принудительно."""
        for key in list(self._selector.get_map().values()):
            port = key.data
            if not isinstance(port, (_ReadPort, _WritePort)):
                continue
            if port.stage is not entry:
                continue

            self._selector.unregister(key.fd)
            if isinstance(port, _WritePort):
                entry.channels.close_writer(port.channel)
                continue
            self._close_sink(port.sink)

        for port in list(self._paused):
            if port.stage is not entry:
                continue
            self._paused.remove(port)
            port.paused = False
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

        if isinstance(port, _WritePort):
            self._pump_write(port)
            return

        self._pump_edge(port)

    def _drain_wake(self) -> None:
        try:
            while os.read(self._wake_read, 4096):
                pass
        except BlockingIOError:
            return

    def _pump_read(self, port: _ReadPort) -> None:
        flow = self._flow_of(port.sink)

        exhausted = False
        if flow is not None:
            exhausted = flow.exhausted

        if exhausted:
            self._close_read_early(port)
            return

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

        saturated = False
        if flow is not None:
            saturated = flow.saturated

        if saturated:
            self._pause_read(port)

    def _pause_read(self, port: _ReadPort) -> None:
        """Буфер ребра полон: чтение источника снимается с selector'а до долива."""
        if port.paused:
            return

        self._selector.unregister(port.fd)
        port.paused = True
        self._paused.append(port)

    def _resume_reads(self) -> None:
        for port in list(self._paused):
            flow = self._flow_of(port.sink)

            saturated = False
            if flow is not None:
                saturated = flow.saturated

            if saturated:
                continue

            self._paused.remove(port)
            port.paused = False
            self._selector.register(port.fd, selectors.EVENT_READ, port)

    def _check_aborts(self) -> None:
        """Источники, все рёбра которых закрылись, снимаются с чтения (EPIPE им)."""
        ports: list[_ReadPort] = []
        for key in list(self._selector.get_map().values()):
            if isinstance(key.data, _ReadPort):
                ports.append(key.data)
        ports.extend(self._paused)

        for port in ports:
            flow = self._flow_of(port.sink)
            if flow is None:
                continue
            if not flow.exhausted:
                continue
            self._close_read_early(port)

    def _close_read_early(self, port: _ReadPort) -> None:
        """Потребители ушли: чтение канала закрывается, источник получит EPIPE."""
        if port.paused:
            self._paused.remove(port)
            port.paused = False
        else:
            self._selector.unregister(port.fd)

        port.stage.channels.close_reader(port.channel)
        port.stage.pending -= 1
        self._close_sink(port.sink)

    def _edge_feed(self, state: _EdgeState, data: bytes) -> None:
        if not state.fd_open:
            # потребитель уже ушёл: байты теряются законно (правило rc 141)
            return

        state.buf.extend(data)

        if state.registered:
            return

        self._selector.register(state.fd, selectors.EVENT_WRITE, state)
        state.registered = True

    def _edge_eof(self, state: _EdgeState) -> None:
        """EOF источника: write-конец закрывается сразу либо после долива буфера."""
        state.eof = True

        if not state.fd_open:
            return

        if state.buf:
            return

        self._edge_close(state)

    def _edge_close(self, state: _EdgeState) -> None:
        if not state.fd_open:
            return

        if state.registered:
            self._selector.unregister(state.fd)
            state.registered = False

        os.close(state.fd)
        state.fd_open = False

    def _edge_broken(self, state: _EdgeState) -> None:
        """Потребитель закрыл чтение до EOF: ребро помечается и закрывается."""
        state.closed_early = True
        state.buf.clear()
        self._edge_close(state)

        self._check_aborts()
        self._resume_reads()

    def _pump_edge(self, state: _EdgeState) -> None:
        try:
            sent = os.write(state.fd, memoryview(state.buf))
        except BlockingIOError:
            return
        except BrokenPipeError:
            self._edge_broken(state)
            return
        except OSError as exc:
            msg = f"edge {state.name} write failed: {exc}"
            raise LauncherError(msg) from exc

        state.delivered += sent
        del state.buf[:sent]

        if state.buf:
            if len(state.buf) < state.limit:
                self._resume_reads()
            return

        self._selector.unregister(state.fd)
        state.registered = False

        if state.eof:
            self._edge_close(state)

        self._resume_reads()

    @staticmethod
    def _flow_of(sink: ChannelSink | None) -> PipeSink | FanoutSink | None:
        if isinstance(sink, (PipeSink, FanoutSink)):
            return sink

        return None

    @staticmethod
    def _stage_edges(sinks: Mapping[Channel, ChannelSink]) -> tuple[PipeSink, ...]:
        edges: list[PipeSink] = []
        for sink in sinks.values():
            if isinstance(sink, PipeSink):
                edges.append(sink)
                continue
            if isinstance(sink, FanoutSink):
                edges.extend(sink.pipes())

        return tuple(edges)

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

        # завершение, не увиденное циклом, фиксируется временем уборки
        now = time.monotonic()
        for entry in self._stages.values():
            if entry.ended is None:
                entry.ended = now

        for sink in list(self._open_sinks):
            self._open_sinks.remove(sink)
            try:
                sink.close()
            except Exception:
                logger.exception("channel sink close failed during pump shutdown")

        for state in self._edges.values():
            if not state.fd_open:
                continue
            if state.registered:
                self._selector.unregister(state.fd)
                state.registered = False
            os.close(state.fd)
            state.fd_open = False

        with self._wake_lock:
            self._wake_open = False
            os.close(self._wake_write)

        self._selector.close()
        os.close(self._wake_read)


class PipeSink(ChannelSink):
    """Ребро графа: байты источника в write-конец pipe'а потребителя.

    Создаётся насосом (`ChannelPump.add_edge`), который владеет дескриптором и
    пишет через selector с ограниченным буфером. Sink пути данных: его ошибки
    фатальны для запуска; обрыв потребителем ошибкой не считается и оседает
    признаком `broken`.
    """

    def __init__(self, pump: ChannelPump, state: _EdgeState) -> None:
        self._pump = pump
        self._state = state

    @property
    def edge(self) -> str:
        return self._state.name

    @property
    def delivered(self) -> int:
        """Байты, дошедшие до потребителя."""
        return self._state.delivered

    @property
    def broken(self) -> bool:
        """Потребитель закрыл чтение до EOF источника."""
        return self._state.closed_early

    @property
    def saturated(self) -> bool:
        """Буфер полон: чтение источника пора приостановить."""
        state = self._state

        if not state.fd_open:
            return False

        return len(state.buf) >= state.limit

    @property
    def exhausted(self) -> bool:
        """Кормить ребро больше некого: потребитель ушёл."""
        return self._state.closed_early

    def feed(self, data: bytes) -> None:
        self._pump._edge_feed(self._state, data)

    def close(self) -> None:
        self._pump._edge_eof(self._state)


class FanoutSink(ChannelSink):
    """Веер: копия байтов источника каждому приёмнику.

    Скорость задаёт медленнейший потребитель (backpressure по любому полному
    буферу), а обрыв источника наступает только когда закрылись все рёбра.
    Побочные приёмники в составе веера глушат свои ошибки сами.
    """

    def __init__(self, sinks: Sequence[ChannelSink]) -> None:
        if not sinks:
            raise LauncherError("fanout needs at least one sink")

        self._sinks = tuple(sinks)

    def feed(self, data: bytes) -> None:
        for sink in self._sinks:
            sink.feed(data)

    def close(self) -> None:
        for sink in self._sinks:
            sink.close()

    def pipes(self) -> tuple[PipeSink, ...]:
        """Все рёбра веера, включая вложенные вееры."""
        pipes: list[PipeSink] = []
        for sink in self._sinks:
            if isinstance(sink, PipeSink):
                pipes.append(sink)
                continue
            if isinstance(sink, FanoutSink):
                pipes.extend(sink.pipes())

        return tuple(pipes)

    @property
    def saturated(self) -> bool:
        """Полный буфер любого ребра тормозит весь веер."""
        full = False
        for pipe in self.pipes():
            if pipe.saturated:
                full = True

        return full

    @property
    def exhausted(self) -> bool:
        """EPIPE источнику — только когда закрылись все рёбра веера."""
        pipes = self.pipes()

        if not pipes:
            return False

        gone = True
        for pipe in pipes:
            if not pipe.broken:
                gone = False

        return gone


class ToolCallContext(BaseModel):
    """Контекст вызова инструмента: адресат журнала и метка вызова в логах.

    Ставится обвязкой на входе вызова и доезжает копией контекста в поток
    инструмента; ниже по стеку его читает реализация порта запуска. Сегменты
    проверяются здесь же: адрес журнала собирается из них без доработки.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    _CURRENT: ClassVar[ContextVar[ToolCallContext]] = ContextVar("tool_call_context")

    user_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1, max_length=255)
    tool: str = Field(min_length=1)

    @field_validator("user_id", "thread_id")
    @classmethod
    def _path_segment(cls, value: str) -> str:
        return SafeSegment.path(value)

    @field_validator("call_id")
    @classmethod
    def _name_segment(cls, value: str) -> str:
        return SafeSegment.name(value)

    @classmethod
    def set(cls, context: ToolCallContext) -> Token[ToolCallContext]:
        return cls._CURRENT.set(context)

    @classmethod
    def reset(cls, token: Token[ToolCallContext]) -> None:
        cls._CURRENT.reset(token)

    @classmethod
    def current(cls) -> ToolCallContext:
        """Контекст текущего вызова; не поставлен — LauncherError."""
        try:
            return cls._CURRENT.get()
        except LookupError as exc:
            raise LauncherError("tool call context is not set") from exc

    def key(self, stage: str, channel: Channel) -> StreamKey:
        """Адрес журнала канала стадии этого вызова."""
        return StreamKey(
            user_id=self.user_id,
            thread_id=self.thread_id,
            call_id=self.call_id,
            stage=stage,
            channel=channel,
        )

    @property
    def prefix(self) -> str:
        """Префикс файлов вызова в томе: единица учёта и защиты."""
        return StreamKey.prefix_of(self.thread_id, self.call_id)
