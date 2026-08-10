"""Точка входа payload'а: запрос со stdin, кадры и трейлер в stdout.

Логи инструмента едут кадрами в stderr: stdout занят данными, а stderr на
исход операции не влияет — его читает только релей хоста и сливает в общий
журнал приложения. Инструменту достаточно обычного logging.getLogger.

Ошибки делятся на два класса. Ожидаемые — объявленные в PayloadOps.EXPECTED
типы и PayloadError — уходят кадром `sandbox-error:` с готовым для пользователя
текстом; трейсбек в этом случае не печатается. Всё остальное не ловится и
падает штатным трейсбеком интерпретатора: неизвестную ошибку прятать нельзя.
Код возврата в обоих случаях ненулевой — отказ остаётся отказом.

Канальная сторона (PayloadChannels): запрос из tool_args, данные сырыми
байтами в tool_payload, конверт результата в tool_result; номера дескрипторов
приходят в env по Channel.env_name.

Ошибки канальной стороны: ChannelError — нарушение контракта каналов;
PayloadOutputClosed — потребитель канала данных закрыл чтение (сигнал
остановить продукцию, не отказ); SystemExit(PayloadExit.FAILURE) — битый
запрос, конверт invalid_request уже записан в tool_result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from abc import abstractmethod
from collections.abc import Callable, Coroutine, Mapping
from enum import IntEnum
from typing import Any, BinaryIO, ClassVar, NoReturn, Protocol, TypeAlias, TypeVar, overload

from pydantic import BaseModel, ValidationError

from boba.toolkit.channels import (
    Channel,
    ChannelError,
    ResultError,
    ResultFailure,
    ResultSuccess,
)
from boba.toolkit.launcher import LaunchPayload

__all__ = [
    "ChunkEmitter",
    "PayloadChannels",
    "PayloadEntry",
    "PayloadError",
    "PayloadExit",
    "PayloadLogging",
    "PayloadOps",
    "PayloadOutputClosed",
    "PayloadStream",
]

TModel = TypeVar("TModel", bound=BaseModel)

ChunkEmitter: TypeAlias = Callable[[str], None]


class PayloadLogFormatter(logging.Formatter):
    """Запись логера -> кадр `sandbox-log:` одной строкой."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return LaunchPayload.encode_log(record.levelname, record.name, message)


class PayloadLogging:
    """Логер payload'а: кадры в stderr вместо свободного текста.

    Уровень не настраивается здесь: его вычисляет хост из своей секции logger
    и передаёт переменной окружения — так у настройки остаётся один источник.
    """

    LEVEL_ENV: ClassVar[str] = "BOBA_LOG_LEVEL"
    """Канал доставки уровня от хоста; в конфиге такой ручки нет."""

    FALLBACK_LEVEL: ClassVar[str] = "INFO"
    """Только для запуска payload'а руками, без хоста."""

    @classmethod
    def setup(cls) -> None:
        """Ставится один раз на процесс; уровень приходит от хоста."""
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(PayloadLogFormatter())
        logging.basicConfig(level=cls.level(), handlers=[handler], force=True)

    @classmethod
    def level(cls) -> int:
        raw = os.environ.get(cls.LEVEL_ENV, cls.FALLBACK_LEVEL).upper()
        resolved = logging.getLevelName(raw)
        if isinstance(resolved, int):
            return resolved
        return logging.INFO


class PayloadError(Exception):
    """Ожидаемая ошибка операции с готовой формулировкой для пользователя."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class PayloadOps(Protocol):
    """Контракт payload'а: операции плюс объявленные ожидаемые ошибки.

    EXPECTED перечисляет типы, которые для этого инструмента являются штатным
    отказом: их текст едет пользователю без трейсбека. Пустая мапа означает,
    что ожидаемых ошибок у инструмента нет.
    """

    EXPECTED: ClassVar[Mapping[type[Exception], str]]

    @classmethod
    @abstractmethod
    def dispatch(
        cls,
        request: dict[str, Any],
        emit: ChunkEmitter,
    ) -> Coroutine[Any, Any, dict[str, Any]]:
        """Выполнить операцию запроса; данные уходят через emit."""
        ...


class PayloadEntry:
    """Разбор запроса, печать кадров и трейлера; операцию выбирает инструмент."""

    CHUNK_CHARS: ClassVar[int] = 64 * 1024

    FAILURE_CODE: ClassVar[int] = 1

    BUILTIN_EXPECTED: ClassVar[Mapping[type[Exception], str]] = {
        ValidationError: "invalid_request",
    }
    """Ожидаемое для любого payload'а: запрос не по контракту."""

    @staticmethod
    def emit_text(emit: ChunkEmitter, text: str) -> None:
        """Материализованный текст уходит кадрами ограниченного размера."""
        for start in range(0, len(text), PayloadEntry.CHUNK_CHARS):
            emit(text[start : start + PayloadEntry.CHUNK_CHARS])

    @staticmethod
    def main(ops: type[PayloadOps]) -> int:
        PayloadLogging.setup()
        request = json.loads(sys.stdin.read())
        try:
            trailer = asyncio.run(ops.dispatch(request, PayloadEntry.emit))
        except PayloadError as e:
            PayloadEntry._write_error(e.kind, e.message)
            return PayloadEntry.FAILURE_CODE
        except Exception as e:
            kind = PayloadEntry._expected_kind(ops, e)
            if kind is None:
                raise
            PayloadEntry._write_error(kind, PayloadEntry._reason(e))
            return PayloadEntry.FAILURE_CODE

        PayloadEntry._write_trailer(trailer)
        return 0

    @staticmethod
    def emit(chunk: str) -> None:
        """Кадр уходит сразу: flush отдаёт данные хосту, не дожидаясь конца."""
        sys.stdout.write(LaunchPayload.encode_chunk(chunk))
        sys.stdout.write("\n")
        sys.stdout.flush()

    @staticmethod
    def _expected_kind(ops: type[PayloadOps], error: Exception) -> str | None:
        """Kind объявленного типа; учитываются и подклассы объявленного."""
        for source in (ops.EXPECTED, PayloadEntry.BUILTIN_EXPECTED):
            for declared, kind in source.items():
                if isinstance(error, declared):
                    return kind
        return None

    @staticmethod
    def _reason(error: Exception) -> str:
        """Текст исключения; у части библиотечных ошибок он пуст — тогда имя типа."""
        text = str(error).strip()
        if text:
            return f"{type(error).__name__}: {text}"
        return type(error).__name__

    @staticmethod
    def _write_error(kind: str, message: str) -> None:
        """Кадр ошибки хосту; в журнал тот же факт уходит обычным логом."""
        if not message.strip():
            message = kind
        sys.stdout.write(LaunchPayload.encode_error(kind, message))
        sys.stdout.write("\n")
        sys.stdout.flush()
        logging.getLogger(__name__).error("%s: %s", kind, message)

    @staticmethod
    def _write_trailer(trailer: dict[str, Any]) -> None:
        body = json.dumps(trailer, ensure_ascii=False)
        sys.stdout.write(LaunchPayload.MARKER)
        sys.stdout.write(body)
        sys.stdout.write("\n")
        sys.stdout.flush()


class PayloadExit(IntEnum):
    """Коды возврата канального payload'а; 141 — шелльная форма SIGPIPE."""

    OK = 0
    FAILURE = 1
    CONSUMER_GONE = 141


class PayloadOutputClosed(Exception):
    """Потребитель tool_payload закрыл чтение: продукцию нужно прекратить.

    Не отказ операции: квитанция пишется в tool_result, процесс завершается
    кодом PayloadExit.CONSUMER_GONE.
    """


class PayloadStream:
    """Канал данных tool_payload: счёт bytes_out и перевод EPIPE в остановку."""

    def __init__(self, raw: BinaryIO) -> None:
        self._raw = raw
        self._bytes_out = 0
        self._consumer_gone = False

    @property
    def bytes_out(self) -> int:
        return self._bytes_out

    @property
    def consumer_gone(self) -> bool:
        return self._consumer_gone

    def write(self, data: bytes) -> int:
        if self._consumer_gone:
            raise PayloadOutputClosed("payload consumer is gone")

        try:
            written = self._raw.write(data)
        except BrokenPipeError as exc:
            self._consumer_gone = True
            raise PayloadOutputClosed("payload consumer closed the stream") from exc

        self._bytes_out += written

        return written

    def flush(self) -> None:
        if self._consumer_gone:
            return

        try:
            self._raw.flush()
        except BrokenPipeError as exc:
            self._consumer_gone = True
            raise PayloadOutputClosed("payload consumer closed the stream") from exc

    def close(self) -> None:
        """Закрытие после ухода потребителя не отказ: факт остаётся в флаге."""
        try:
            self._raw.close()
        except BrokenPipeError:
            self._consumer_gone = True


class PayloadChannels:
    """Сторона инструмента внутри песочницы: доступ к каналам запуска.

    Дескрипторы разбираются из env один раз на процесс; обязательный минимум —
    tool_args, tool_result, tool_stdout, tool_stderr. Опциональные каналы
    существуют по наличию переменной, обращение к необъявленному — ChannelError.
    """

    ENCODING: ClassVar[str] = "utf-8"

    INVALID_REQUEST: ClassVar[str] = "invalid_request"

    _TOOL_SIDE: ClassVar[tuple[Channel, ...]] = (
        Channel.TOOL_ARGS,
        Channel.TOOL_STDIN,
        Channel.TOOL_STDOUT,
        Channel.TOOL_STDERR,
        Channel.TOOL_PAYLOAD,
        Channel.TOOL_RESULT,
    )

    _instance: ClassVar[PayloadChannels | None] = None

    def __init__(self, fds: Mapping[Channel, int]) -> None:
        self._fds = dict(fds)
        self._args_consumed = False
        self._result_written = False
        self._stdin_stream: BinaryIO | None = None
        self._payload_stream: PayloadStream | None = None

    @classmethod
    def open(cls) -> PayloadChannels:
        """Разбор env по Channel.env_name; повторный вызов отдаёт тот же экземпляр."""
        if cls._instance is not None:
            return cls._instance

        fds: dict[Channel, int] = {}

        for channel in cls._TOOL_SIDE:
            raw = os.environ.get(channel.env_name)

            if raw is not None:
                fds[channel] = cls._parse_fd(channel, raw)
                continue

            if channel.is_required:
                raise ChannelError(f"required channel is not declared: {channel.env_name}")

        cls._instance = cls(fds)

        return cls._instance

    @overload
    def args(self, schema: type[TModel]) -> TModel: ...

    @overload
    def args(self) -> dict[str, Any]: ...

    def args(self, schema: type[TModel] | None = None) -> TModel | dict[str, Any]:
        """Один JSON из tool_args; без схемы — разобранный dict (до этапа 3)."""
        raw = self._read_args()

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._fail_invalid_request(f"tool_args is not valid JSON: {exc}", exc)

        if schema is not None:
            return self._validate_args(schema, parsed)

        if not isinstance(parsed, dict):
            reason = f"tool_args must be a JSON object, got {type(parsed).__name__}"
            self._fail_invalid_request(reason, ValueError(reason))

        return parsed

    def stdin(self) -> BinaryIO:
        """Входной поток инструмента; канал опционален."""
        if self._stdin_stream is None:
            fd = self._fd(Channel.TOOL_STDIN)
            self._stdin_stream = os.fdopen(fd, "rb")

        return self._stdin_stream

    def payload(self) -> PayloadStream:
        """Канал данных; обёртка считает bytes_out и переводит EPIPE в остановку."""
        if self._payload_stream is None:
            fd = self._fd(Channel.TOOL_PAYLOAD)
            self._payload_stream = PayloadStream(os.fdopen(fd, "wb"))

        return self._payload_stream

    def write_result(self, data: BaseModel) -> None:
        """Конверт {bytes_out, data} в tool_result; bytes_out подставляется сам."""
        bytes_out = self._flushed_bytes_out()

        envelope = ResultSuccess(bytes_out=bytes_out, data=data.model_dump(mode="json"))

        self._write_envelope(envelope.model_dump_json())

    def write_error(self, kind: str, message: str) -> None:
        """Конверт {error: {kind, message}} в tool_result; тот же факт — в лог."""
        if not message.strip():
            message = kind

        try:
            envelope = ResultFailure(error=ResultError(kind=kind, message=message))
        except ValidationError as exc:
            reason = f"error envelope violates the contract: kind={kind!r}"
            raise ChannelError(reason) from exc

        self._write_envelope(envelope.model_dump_json())
        logging.getLogger(__name__).error("%s: %s", kind, message)

    def exit_code(self) -> PayloadExit:
        """Код завершения процесса по судьбе канала данных."""
        if self._payload_stream is None:
            return PayloadExit.OK

        if self._payload_stream.consumer_gone:
            return PayloadExit.CONSUMER_GONE

        return PayloadExit.OK

    @staticmethod
    def _parse_fd(channel: Channel, raw: str) -> int:
        try:
            return int(raw)
        except ValueError as exc:
            raise ChannelError(
                f"channel env is not a descriptor number: {channel.env_name}={raw!r}"
            ) from exc

    def _fd(self, channel: Channel) -> int:
        fd = self._fds.get(channel)

        if fd is None:
            raise ChannelError(f"channel is not declared for this launch: {channel.env_name}")

        return fd

    def _read_args(self) -> str:
        if self._args_consumed:
            raise ChannelError("tool_args channel is already consumed")

        fd = self._fd(Channel.TOOL_ARGS)
        self._args_consumed = True

        try:
            with os.fdopen(fd, "r", encoding=self.ENCODING) as stream:
                return stream.read()
        except OSError as exc:
            raise ChannelError("tool_args channel is not readable") from exc

    def _validate_args(self, schema: type[TModel], parsed: Any) -> TModel:
        try:
            return schema.model_validate(parsed)
        except ValidationError as exc:
            summary = self._validation_summary(exc)
            reason = f"request does not match {schema.__name__}: {summary}"
            self._fail_invalid_request(reason, exc)

    @staticmethod
    def _validation_summary(error: ValidationError) -> str:
        """Текст без значений полей: tool_args несёт секреты, эхо ввода запрещено."""
        parts: list[str] = []

        for item in error.errors(include_url=False, include_input=False):
            segments: list[str] = []
            for segment in item["loc"]:
                segments.append(str(segment))

            location = ".".join(segments)
            if not location:
                location = "<root>"

            parts.append(f"{location}: {item['msg']}")

        return "; ".join(parts)

    def _fail_invalid_request(self, message: str, cause: Exception) -> NoReturn:
        """Битый запрос — ожидаемый отказ: конверт в tool_result, выход без трейсбека."""
        self.write_error(self.INVALID_REQUEST, message)

        raise SystemExit(int(PayloadExit.FAILURE)) from cause

    def _flushed_bytes_out(self) -> int:
        if self._payload_stream is None:
            return 0

        try:
            self._payload_stream.flush()
        except PayloadOutputClosed:
            # потребитель вышел: байты в буфере pipe потеряны законно, факт остаётся в флаге
            pass

        return self._payload_stream.bytes_out

    def _write_envelope(self, body: str) -> None:
        if self._result_written:
            raise ChannelError("tool_result is already written")

        fd = self._fd(Channel.TOOL_RESULT)
        self._result_written = True

        try:
            with os.fdopen(fd, "w", encoding=self.ENCODING) as stream:
                stream.write(body)
                stream.write("\n")
        except OSError as exc:
            raise ChannelError("tool_result channel is not writable") from exc
