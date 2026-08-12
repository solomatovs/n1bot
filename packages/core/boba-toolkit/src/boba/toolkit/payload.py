"""Точка входа payload'а внутри песочницы: каналы вместо кадров.

Запрос читается одним JSON из tool_args, входной поток — из tool_stdin, данные
уходят сырыми байтами в tool_payload, трейлер либо ожидаемая ошибка — конвертом
в tool_result; номера дескрипторов приходят в env по Channel.env_name. Логи
инструмента едут кадрами LogFrame в stderr — после преамбулы это tool_stderr,
поэтому инструменту достаточно обычного logging.getLogger.

Ошибки операции делятся на два класса. Ожидаемые — объявленные в
PayloadOps.EXPECTED типы и PayloadError — уходят конвертом {error} с готовым
для пользователя текстом; трейсбек не печатается. Всё остальное не ловится и
падает штатным трейсбеком интерпретатора: неизвестную ошибку прятать нельзя.
Код возврата в обоих случаях ненулевой — отказ остаётся отказом.

Ошибки:
ChannelError — нарушение контракта каналов.
PayloadOutputClosedError — потребитель канала данных закрыл чтение (сигнал
    остановить продукцию, не отказ).
SystemExit(PayloadExit.FAILURE) — битый запрос, конверт invalid_request уже
    записан в tool_result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from abc import abstractmethod
from collections.abc import Coroutine, Mapping
from enum import IntEnum, StrEnum
from typing import (
    Any,
    BinaryIO,
    ClassVar,
    NoReturn,
    Protocol,
    TypeVar,
)

from pydantic import BaseModel, ValidationError

from boba.toolkit.channels import (
    Channel,
    ChannelError,
    LogFrame,
    ResultError,
    ResultFailure,
    ResultSuccess,
    ShellExit,
    ValidationSummary,
)
from boba.toolkit.workflow import EmptyTrailer

__all__ = [
    "FailureKind",
    "PayloadChannels",
    "PayloadEntry",
    "PayloadError",
    "PayloadExit",
    "PayloadLogging",
    "PayloadOps",
    "PayloadOutputClosedError",
    "PayloadStream",
]

TModel = TypeVar("TModel", bound=BaseModel)


class FailureKind(StrEnum):
    """Виды отказов конверта tool_result: словарь kind'ов общий для инструментов."""

    INVALID_REQUEST = "invalid_request"
    UNAVAILABLE = "database_unavailable"
    SQL_FAILED = "sql_failed"
    NO_INPUT = "no_input"


class PayloadLogFormatter(logging.Formatter):
    """Запись логера -> лог-кадр LogFrame одной строкой."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"

        frame = LogFrame(lvl=record.levelname, name=record.name, msg=message)

        return frame.encode()


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


class PayloadExit(IntEnum):
    """Коды возврата канального payload'а; CONSUMER_GONE — шелльная форма SIGPIPE."""

    OK = 0
    FAILURE = 1
    CONSUMER_GONE = ShellExit.SIGPIPE


class PayloadOutputClosedError(Exception):
    """Потребитель tool_payload закрыл чтение: продукцию нужно прекратить.

    Не отказ операции: квитанция пишется в tool_result, процесс завершается
    кодом PayloadExit.CONSUMER_GONE. Операция, которой есть что сообщить и
    после обрыва, ловит исключение сама и отдаёт свой трейлер.
    """


class PayloadOps(Protocol):
    """Контракт payload'а: реестр операций, ожидаемые ошибки, диспетчеризация.

    REQUESTS отображает значение поля op запроса в модель запроса операции —
    тот же реестр питает валидацию args узла в графе. EXPECTED перечисляет
    типы, которые для инструмента являются штатным отказом: их текст едет
    пользователю без трейсбека; пустая мапа — ожидаемых ошибок нет.
    """

    EXPECTED: ClassVar[Mapping[type[Exception], str]]

    REQUESTS: ClassVar[Mapping[str, type[BaseModel]]]

    @classmethod
    @abstractmethod
    def dispatch(
        cls,
        request: BaseModel,
        channels: PayloadChannels,
    ) -> Coroutine[Any, Any, BaseModel]:
        """Выполнить операцию валидированного запроса; итог — модель трейлера.

        Данные уходят в channels.payload(), вход читается из channels.stdin().
        """
        ...


class PayloadEntry:
    """Разбор запроса из tool_args, диспетчеризация, квитанция в tool_result."""

    BUILTIN_EXPECTED: ClassVar[Mapping[type[Exception], str]] = {
        ValidationError: FailureKind.INVALID_REQUEST,
    }
    """Ожидаемое для любого payload'а: данные не по контракту."""

    @staticmethod
    def main(ops: type[PayloadOps]) -> int:
        PayloadLogging.setup()

        channels = PayloadChannels.open()

        request = channels.request(ops.REQUESTS)

        try:
            trailer = asyncio.run(ops.dispatch(request, channels))
        except PayloadOutputClosedError:
            # потребитель ушёл на середине: операция не закончена, поэтому
            # квитанция вырожденная — схему трейлера узла ей взять неоткуда
            channels.write_result(EmptyTrailer())
            return int(PayloadExit.CONSUMER_GONE)
        except PayloadError as e:
            channels.write_error(e.kind, e.message)
            return int(PayloadExit.FAILURE)
        except Exception as e:
            kind = PayloadEntry._expected_kind(ops, e)
            if kind is None:
                raise
            channels.write_error(kind, PayloadEntry._reason(e))
            return int(PayloadExit.FAILURE)

        channels.write_result(trailer)

        return int(channels.exit_code())

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
        """Текст исключения; ValidationError сжимается сводкой без значений полей."""
        if isinstance(error, ValidationError):
            summary = ValidationSummary.of(error)
            return f"{type(error).__name__}: {summary}"

        text = str(error).strip()
        if text:
            return f"{type(error).__name__}: {text}"

        return type(error).__name__


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
            raise PayloadOutputClosedError("payload consumer is gone")

        try:
            written = self._raw.write(data)
        except BrokenPipeError as exc:
            self._consumer_gone = True
            raise PayloadOutputClosedError(
                "payload consumer closed the stream"
            ) from exc

        self._bytes_out += written

        return written

    def flush(self) -> None:
        if self._consumer_gone:
            return

        try:
            self._raw.flush()
        except BrokenPipeError as exc:
            self._consumer_gone = True
            raise PayloadOutputClosedError(
                "payload consumer closed the stream"
            ) from exc

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

    OP_KEY: ClassVar[str] = "op"
    """Поле запроса, по которому реестр операций выбирает модель."""

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
                raise ChannelError(
                    f"required channel is not declared: {channel.env_name}"
                )

        cls._instance = cls(fds)

        return cls._instance

    def args(self, schema: type[TModel]) -> TModel:
        """Один JSON из tool_args, валидированный моделью запроса."""
        parsed = self._parsed_args()

        return self._validate_args(schema, parsed)

    def request(self, registry: Mapping[str, type[BaseModel]]) -> BaseModel:
        """Один JSON из tool_args; модель выбирается по полю op из реестра."""
        parsed = self._parsed_args()

        if not isinstance(parsed, dict):
            reason = f"tool_args must be a JSON object, got {type(parsed).__name__}"
            self._fail_invalid_request(reason, ValueError(reason))

        op = parsed.get(self.OP_KEY)
        if not isinstance(op, str):
            reason = f"request field {self.OP_KEY!r} is missing or not a string"
            self._fail_invalid_request(reason, ValueError(reason))

        schema = registry.get(op)
        if schema is None:
            known = ", ".join(sorted(registry))
            reason = f"unknown op {op!r}; known ops: {known}"
            self._fail_invalid_request(reason, ValueError(reason))

        return self._validate_args(schema, parsed)

    def has(self, channel: Channel) -> bool:
        """Канал объявлен для этого запуска: вход есть при ребре или литерале."""
        return channel in self._fds

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
            raise ChannelError(
                f"channel is not declared for this launch: {channel.env_name}"
            )

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

    def _parsed_args(self) -> Any:
        raw = self._read_args()

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            self._fail_invalid_request(f"tool_args is not valid JSON: {exc}", exc)

    def _validate_args(self, schema: type[TModel], parsed: Any) -> TModel:
        try:
            return schema.model_validate(parsed)
        except ValidationError as exc:
            # текст без значений полей: tool_args несёт секреты, эхо ввода запрещено
            summary = ValidationSummary.of(exc)
            reason = f"request does not match {schema.__name__}: {summary}"
            self._fail_invalid_request(reason, exc)

    def _fail_invalid_request(self, message: str, cause: Exception) -> NoReturn:
        """
        Битый запрос — ожидаемый отказ: конверт в tool_result, выход без трейсбека
        """
        self.write_error(FailureKind.INVALID_REQUEST, message)

        raise SystemExit(int(PayloadExit.FAILURE)) from cause

    def _flushed_bytes_out(self) -> int:
        if self._payload_stream is None:
            return 0

        try:  # noqa: SIM105
            self._payload_stream.flush()
        except PayloadOutputClosedError:
            # потребитель вышел: байты в буфере pipe потеряны законно,
            # факт остаётся в флаге
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
