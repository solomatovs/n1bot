"""Контракт каналов sandbox-запуска: реестр каналов, имена env, кодек лог-кадров,
форматы потоков с их чтением и записью, конверты tool_result и wrap_result,
шелльная форма кодов возврата, протокол приёмника байтов, построчный разборщик
и сводка ошибок валидации без эха ввода.

Доменный слой: транспорт и I/O не импортируются, дескрипторы открывает исполнитель.

Ошибки: ChannelError — нарушение контракта каналов.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import Iterator, Mapping
from enum import StrEnum, nonmember
from typing import Any, BinaryIO, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

__all__ = [
    "ByteText",
    "Channel",
    "ChannelError",
    "ChannelSink",
    "LineSplitter",
    "LogFrame",
    "ResultError",
    "ResultFailure",
    "ResultSuccess",
    "ShellExit",
    "StageExit",
    "StreamCodec",
    "StreamFormat",
    "ValidationSummary",
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
    """Формат данных потокового канала (MIME); чтение и запись — StreamCodec."""

    CSV = "text/csv"
    NDJSON = "application/x-ndjson"
    TEXT = "text/plain"
    BYTES = "application/octet-stream"

    @property
    def is_text(self) -> bool:
        """Формат читается как текст; BYTES — непрозрачные байты."""
        return self is not StreamFormat.BYTES


class ByteText(StrEnum):
    """Параметры декодирования байтовых каналов в текст."""

    ENCODING = "utf-8"
    ERRORS = "replace"


class StreamCodec:
    """Чтение и запись форматов канала данных: один кодек на все инструменты.

    Формат объявлен до запуска — `contract.out` у источника, `stdin_format` у
    приёмника, — поэтому своих парсеров у инструментов нет и нюхать байты
    некому. Записи строчного потока (NDJSON) — одна запись-словарь на строку.
    """

    ROWS: ClassVar[StreamFormat] = StreamFormat.NDJSON
    """Формат строчного потока, который кодируют encode_row/decode_row."""

    LINE_END: ClassVar[str] = "\n"

    @classmethod
    def encode_text(cls, fmt: StreamFormat, text: str) -> bytes:
        """Текстовый продукт в байты канала; двоичный формат текстом не пишется."""
        cls._require_text(fmt)

        return text.encode(ByteText.ENCODING)

    @classmethod
    def read_text(cls, fmt: StreamFormat, source: BinaryIO) -> str:
        """Весь входной поток объявленного формата как текст."""
        cls._require_text(fmt)

        return source.read().decode(ByteText.ENCODING, errors=ByteText.ERRORS)

    @classmethod
    def encode_row(cls, row: Mapping[str, Any]) -> bytes:
        """Одна запись строчного потока: JSON плюс перевод строки."""
        body = json.dumps(row, ensure_ascii=False)

        return (body + cls.LINE_END).encode(ByteText.ENCODING)

    @classmethod
    def decode_row(cls, line: str) -> dict[str, Any]:
        """Разбор строки строчного потока; не объект — нарушение контракта."""
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            msg = f"row line is not valid JSON: {exc}"
            raise ChannelError(msg) from exc

        if not isinstance(row, dict):
            msg = f"row line must be a JSON object, got {type(row).__name__}"
            raise ChannelError(msg)

        return row

    @staticmethod
    def _require_text(fmt: StreamFormat) -> None:
        if fmt.is_text:
            return

        raise ChannelError(f"stream format is not text: {fmt}")


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
            lines.append(raw.decode(ByteText.ENCODING, errors=ByteText.ERRORS))

        return iter(lines)

    def flush(self) -> Iterator[str]:
        """Последняя строка без перевода; после вызова буфер пуст."""
        if not self._tail:
            return iter(())

        line = bytes(self._tail).decode(ByteText.ENCODING, errors=ByteText.ERRORS)
        self._tail.clear()

        return iter((line,))


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
    SIGPIPE: ClassVar[int] = 141
    _SIGNAL_BASE: ClassVar[int] = 128

    @classmethod
    def of(cls, returncode: int | None) -> int:
        """Код subprocess/Popen; None — процесс убит до опроса."""
        if returncode is None:
            return cls.KILLED

        if returncode < 0:
            return cls._SIGNAL_BASE - returncode

        return returncode


class StageExit(BaseModel):
    """Строка канала wrap_result: итог одной стадии mount-группы `{stage, rc}`.

    rc уже нормализован к шелльной форме (128+N для сигналов); кодек один на
    обе стороны канала — пишет лаунчер, читает раннер.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ENCODING: ClassVar[str] = "utf-8"

    stage: str = Field(min_length=1)
    rc: int = Field(ge=0)

    def encode_line(self) -> bytes:
        return self.model_dump_json().encode(self.ENCODING) + b"\n"

    @classmethod
    def decode_line(cls, line: str) -> StageExit:
        """Разбор строки канала; битая строка — ChannelError."""
        try:
            return cls.model_validate_json(line)
        except ValidationError as exc:
            raise ChannelError(f"broken wrap_result line: {line[:120]!r}") from exc


class ValidationSummary:
    """Сводка ValidationError без значений полей: вход может нести секреты."""

    ROOT: ClassVar[str] = "<root>"

    @classmethod
    def of(cls, error: ValidationError) -> str:
        parts: list[str] = []

        for item in error.errors(include_url=False, include_input=False):
            segments: list[str] = []
            for segment in item["loc"]:
                segments.append(str(segment))

            location = ".".join(segments)
            if not location:
                location = cls.ROOT

            parts.append(f"{location}: {item['msg']}")

        return "; ".join(parts)


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
