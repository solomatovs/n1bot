"""Контракт каналов sandbox-запуска: реестр каналов, имена env, кодек лог-кадров,
объявление формата продукта и кодек границы payload'а, конверты tool_result и
wrap_result, шелльная форма кодов возврата, протокол приёмника байтов,
построчный разборщик, адрес журнала канала и сводка ошибок валидации без эха ввода.

По каналам текут байты: декодирование в текст живёт только на границах — у
payload'а, у коллекторов вызывающего и в разборе лог-кадров tool_stderr.

Доменный слой: транспорт и I/O не импортируются, дескрипторы открывает исполнитель.

Ошибки: ChannelError — нарушение контракта каналов.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import Iterator, Mapping
from enum import StrEnum, nonmember
from typing import Any, BinaryIO, ClassVar, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
)

__all__ = [
    "ByteText",
    "Channel",
    "ChannelError",
    "ChannelSink",
    "JournalFile",
    "LineSplitter",
    "LogFrame",
    "ResultError",
    "ResultFailure",
    "ResultSuccess",
    "SafeSegment",
    "ShellExit",
    "StageExit",
    "StreamCodec",
    "StreamFormat",
    "StreamKey",
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
        """Обязательный минимум любого узла: остальные каналы объявляет исполнитель."""
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
    """Объявленный формат продукта узла (MIME): декларация для панели и человека.

    Каналом текут байты: формат ничего не валидирует и путь данных не меняет.
    """

    CSV = "text/csv"
    NDJSON = "application/x-ndjson"
    TEXT = "text/plain"
    BYTES = "application/octet-stream"


class ByteText(StrEnum):
    """Параметры декодирования байтовых каналов в текст."""

    ENCODING = "utf-8"
    ERRORS = "replace"


class StreamCodec:
    """Кодек границы payload'а: текст и строчные записи в байты канала и обратно.

    Путь данных байтовый — кодек зовёт сам инструмент, когда его собственный
    контракт говорит трактовать байты текстом. Записи строчного потока
    (NDJSON) — одна запись-словарь на строку.
    """

    ROWS: ClassVar[StreamFormat] = StreamFormat.NDJSON
    """Формат строчного потока, который кодируют encode_row/decode_row."""

    LINE_END: ClassVar[str] = "\n"

    @classmethod
    def encode_text(cls, text: str) -> bytes:
        """Текст инструмента в байты канала."""
        return text.encode(ByteText.ENCODING)

    @classmethod
    def read_text(cls, source: BinaryIO) -> str:
        """Весь входной поток как текст."""
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


class SafeSegment:
    """Алфавиты сегментов адреса журнала: каталог тома и сегмент имени файла.

    В имени файла точка разделяет сегменты, поэтому внутри call_id и stage она
    запрещена: иначе имя неразложимо, а защита живого вызова по префиксу
    `{call_id}.` накрывала бы чужие файлы.
    """

    PATH: ClassVar[frozenset[str]] = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    )
    NAME: ClassVar[frozenset[str]] = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    )

    @classmethod
    def path(cls, value: str) -> str:
        """Сегмент пути тома: точка допустима, ведущая — нет."""
        unsafe = set(value) - cls.PATH
        if unsafe:
            raise ValueError(f"unsafe characters in path segment: {sorted(unsafe)}")

        if value.startswith("."):
            raise ValueError(f"path segment must not start with a dot: {value!r}")

        return value

    @classmethod
    def name(cls, value: str) -> str:
        """Сегмент имени файла журнала: точка запрещена."""
        unsafe = set(value) - cls.NAME
        if unsafe:
            raise ValueError(f"unsafe characters in name segment: {sorted(unsafe)}")

        return value


class JournalFile(StrEnum):
    """Расширения файлов журнала; имена собирает и разбирает StreamKey."""

    LOG = "log"
    META = "meta.json"
    TMP = "tmp"

    SEP = nonmember(".")

    @classmethod
    def is_log(cls, file_name: str) -> bool:
        return file_name.endswith(f"{cls.SEP}{cls.LOG}")

    @classmethod
    def tmp_of(cls, path: str, pid: int) -> str:
        """Черновик сайдкара: подменяется на месте через rename."""
        return f"{path}{cls.SEP}{cls.TMP}{cls.SEP}{pid}"

    @classmethod
    def body_of(cls, file_name: str) -> tuple[str, ...]:
        """Сегменты имени без расширения; чужое имя — ChannelError."""
        segments = tuple(file_name.split(cls.SEP))

        for suffix in (cls.LOG, cls.META):
            tail = tuple(suffix.split(cls.SEP))
            if segments[-len(tail) :] == tail:
                return segments[: -len(tail)]

        raise ChannelError(f"not a journal file name: {file_name!r}")


class StreamKey(BaseModel):
    """Адрес журнала одного канала стадии в служебном томе пользователя.

    Файл — `{thread_id}/{call_id}.{stage}.{channel}.log`, сайдкар — то же имя
    с расширением сайдкара. Имя разбирается по сегментам с конца: расширение,
    канал (член Channel), стадия (id узла), остаток — вызов; срезом суффикса —
    никогда. call_id приходит из протокола LLM-провайдера, в путь допускаются
    только безопасные символы.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    BODY_SEGMENTS: ClassVar[int] = 3
    """Сегментов имени до расширения: call_id, stage, channel."""

    user_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1, max_length=255)
    stage: str = Field(min_length=1, max_length=64)
    channel: Channel

    @field_validator("user_id", "thread_id")
    @classmethod
    def _path_segment(cls, value: str) -> str:
        return SafeSegment.path(value)

    @field_validator("call_id", "stage")
    @classmethod
    def _name_segment(cls, value: str) -> str:
        return SafeSegment.name(value)

    @classmethod
    def prefix_of(cls, thread_id: str, call_id: str) -> str:
        """Префикс всех файлов вызова: единица учёта и защиты тома."""
        return f"{thread_id}/{call_id}{JournalFile.SEP}"

    @property
    def call_prefix(self) -> str:
        return StreamKey.prefix_of(self.thread_id, self.call_id)

    def rel_log(self) -> str:
        return self._rel(JournalFile.LOG)

    def rel_meta(self) -> str:
        return self._rel(JournalFile.META)

    def _rel(self, suffix: JournalFile) -> str:
        body = (self.call_id, self.stage, self.channel.value, suffix.value)
        name = JournalFile.SEP.join(body)

        return f"{self.thread_id}/{name}"

    @classmethod
    def of_file(cls, user_id: str, thread_id: str, file_name: str) -> StreamKey:
        """Разбор имени файла журнала; чужое имя — ChannelError."""
        body = JournalFile.body_of(file_name)

        if len(body) != cls.BODY_SEGMENTS:
            raise ChannelError(f"journal name is not addressable: {file_name!r}")

        call_id, stage, channel_name = body

        try:
            channel = Channel(channel_name)
        except ValueError as exc:
            raise ChannelError(f"unknown journal channel: {file_name!r}") from exc

        try:
            return cls(
                user_id=user_id,
                thread_id=thread_id,
                call_id=call_id,
                stage=stage,
                channel=channel,
            )
        except ValidationError as exc:
            raise ChannelError(
                f"journal name is not addressable: {file_name!r}"
            ) from exc


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
