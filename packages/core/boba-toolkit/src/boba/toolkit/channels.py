"""Контракт каналов sandbox-запуска: реестр каналов, имена env, кодек лог-кадров,
конверт tool_result и шелльная форма кодов возврата.

Доменный слой: транспорт и I/O не импортируются, дескрипторы открывает исполнитель.

Ошибки: ChannelError — нарушение контракта каналов.
"""

from __future__ import annotations

from enum import StrEnum, nonmember
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

__all__ = [
    "Channel",
    "ChannelError",
    "LogFrame",
    "ResultError",
    "ResultFailure",
    "ResultSuccess",
    "ShellExit",
    "StreamFormat",
]


class ChannelError(RuntimeError):
    """Нарушение контракта каналов: данным канала доверять нельзя."""


class Channel(StrEnum):
    """Реестр каналов одного запуска: имя, направление, env-имя дескриптора.

    Номера дескрипторов динамические (кроме stdin/stdout/stderr обвязки) и
    сообщаются инструменту через переменные окружения `env_name`.
    """

    WRAP_ARGS = "wrap_args"
    WRAP_ARGS_INNER = "wrap_args_inner"
    TOOL_ARGS = "tool_args"
    TOOL_STDIN = "tool_stdin"
    TOOL_STDOUT = "tool_stdout"
    TOOL_STDERR = "tool_stderr"
    TOOL_PAYLOAD = "tool_payload"
    TOOL_RESULT = "tool_result"
    WRAP_STDOUT = "wrap_stdout"
    WRAP_STDERR = "wrap_stderr"
    WRAP_RESULT = "wrap_result"

    ENV_PREFIX = nonmember("BOBA_CHANNEL_")

    @property
    def env_name(self) -> str:
        """Переменная окружения, через которую номер дескриптора уезжает внутрь."""
        return Channel.ENV_PREFIX + self.name

    @property
    def writes_in(self) -> bool:
        """True — в канал пишет приложение, песочница читает."""
        inbound = (
            Channel.WRAP_ARGS,
            Channel.WRAP_ARGS_INNER,
            Channel.TOOL_ARGS,
            Channel.TOOL_STDIN,
        )

        return self in inbound

    @property
    def is_required(self) -> bool:
        """Канал обязательного минимума узла."""
        return self in Channel.required()

    @classmethod
    def required(cls) -> frozenset[Channel]:
        """Обязательный минимум любого узла: остальное выводится из контракта стадии."""
        members = (
            cls.WRAP_ARGS,
            cls.TOOL_ARGS,
            cls.TOOL_STDOUT,
            cls.TOOL_STDERR,
            cls.TOOL_RESULT,
            cls.WRAP_STDOUT,
            cls.WRAP_STDERR,
        )

        return frozenset(members)


class StreamFormat(StrEnum):
    """Формат данных потокового канала (MIME); API чтения и записи — этап 3."""

    CSV = "text/csv"
    NDJSON = "application/x-ndjson"
    TEXT = "text/plain"
    BYTES = "application/octet-stream"


class ResultError(BaseModel):
    """Тело ожидаемой ошибки конверта tool_result."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ResultFailure(BaseModel):
    """Конверт отказа канала tool_result: `{"error": {kind, message}}`."""

    model_config = ConfigDict(extra="forbid")

    error: ResultError


class ResultSuccess(BaseModel):
    """Конверт успеха канала tool_result: `{"bytes_out": N, "data": ...}`."""

    model_config = ConfigDict(extra="forbid")

    bytes_out: int = Field(ge=0)
    data: JsonValue


class ShellExit:
    """Шелльная форма кода возврата: убитый сигналом N процесс — 128+N.

    Одно правило для всех сторон контракта: раннер нормализует Popen-код
    стадии, payload — код своего subprocess-ребёнка.
    """

    KILLED: ClassVar[int] = 137
    _SIGNAL_BASE: ClassVar[int] = 128

    @classmethod
    def of(cls, returncode: int | None) -> int:
        """Код subprocess/Popen; None — процесс убит до опроса."""
        if returncode is None:
            return cls.KILLED

        if returncode < 0:
            return cls._SIGNAL_BASE - returncode

        return returncode


class LogFrame(BaseModel):
    """Лог-кадр инструмента: одна строка `tool_stderr` — маркер плюс JSON-тело.

    Единственная in-band разметка контракта: строка с маркером — кадр,
    любая другая — сырой текст канала.
    """

    model_config = ConfigDict(extra="forbid")

    MARKER: ClassVar[str] = "sandbox-log:"

    lvl: str
    name: str
    msg: str

    def encode(self) -> str:
        """Строка кадра: многострочное сообщение экранируется JSON'ом в одну строку."""
        return f"{LogFrame.MARKER}{self.model_dump_json()}"

    @classmethod
    def matches(cls, line: str) -> bool:
        """Строка канала является лог-кадром."""
        return line.startswith(cls.MARKER)

    @classmethod
    def decode(cls, line: str) -> LogFrame:
        """Разбор кадра; строка без маркера или битое тело — ChannelError."""
        if not line.startswith(cls.MARKER):
            raise ChannelError(f"not a log frame: {line[:80]!r}")

        body = line[len(cls.MARKER) :]

        try:
            return cls.model_validate_json(body)
        except ValidationError as exc:
            raise ChannelError(f"broken log frame body: {body[:80]!r}") from exc
