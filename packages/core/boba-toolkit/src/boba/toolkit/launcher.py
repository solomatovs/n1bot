"""Порт запуска инструмента: контракт между инструментами и исполнителями.

Телу инструмента нужен запуск в отдельном процессе, но знать, как тот
устроен (subprocess, bwrap, cgroup), оно не должно. Здесь объявлены
протоколы ToolLauncher (открыть вызов) и ToolCall (один открытый вызов),
модели итога и общие буферы каналов; реализации — ProcessToolCaller
(boba.toolrun.process) и ZygoteToolCaller (boba.sandbox.zygote) —
подставляются снаружи. Вызов всегда потоковый: вход и выход — кадры
(boba.toolkit.frames); накопительный «вызвал и получил итог» строится
поверх него компонентом CollectedCall.

Ошибки:
LauncherError — исполнитель нарушил контракт, результату доверять нельзя.
PayloadFailureError — инструмент сообщил об ожидаемом отказе конвертом.
ChannelOverflowError — канал вызова превысил байтовый потолок.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, ClassVar, Protocol, Self

from pydantic import BaseModel, ConfigDict, TypeAdapter

from boba.toolkit.failure import ToolRefusalError
from boba.toolkit.frames import ToolFrame
from boba.toolkit.protocol import REPLY, ReplyError, ReplyOk, ToolCommand
from boba.toolkit.stream import Chunk

__all__ = [
    "CappedChannel",
    "ChannelOverflowError",
    "ChannelTail",
    "ClippedText",
    "CollectedCall",
    "EnvelopeReply",
    "ErrorKind",
    "LaunchOutcome",
    "LaunchPayload",
    "LauncherError",
    "LauncherFactory",
    "PayloadFailureError",
    "RowStream",
    "RunResult",
    "ToolCall",
    "ToolLauncher",
    "ToolOutcome",
]


class LauncherError(RuntimeError):
    """Исполнитель нарушил контракт: результату доверять нельзя."""


class ChannelOverflowError(LauncherError):
    """Канал вызова превысил байтовый потолок: вызов обрывается."""


class CappedChannel:
    """Буфер, копящий канал целиком, но не длиннее потолка.

    Держит конверт результата и вывод shell-команды. Потолок обязателен:
    вывод тела копится в памяти приложения, на которую лимиты песочницы не
    действуют, и без него болтливое тело вынесло бы хост. Превышение —
    ChannelOverflowError, насос по ней добивает вызов.
    """

    def __init__(self, limit: int, channel: str) -> None:
        self._limit = limit
        self._channel = channel
        self._data = bytearray()

    def feed(self, chunk: Chunk) -> None:
        self._data.extend(chunk)
        if len(self._data) <= self._limit:
            return

        msg = f"{self._channel} exceeded {self._limit} bytes; the call was killed"
        raise ChannelOverflowError(msg)

    def text(self) -> str:
        return self._data.decode("utf-8", errors="replace")

    def data(self) -> bytearray:
        """Конверт как есть: pydantic разбирает bytes-like без копии."""
        return self._data


class ChannelTail:
    """Кольцевой буфер хвоста канала: помнит последние байты, старое
    вытесняется.

    Держит хвост stderr тела — им объясняется сбой, когда вызов кончился
    без конверта результата.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._tail = bytearray()

    def feed(self, chunk: Chunk) -> None:
        self._tail.extend(chunk)
        if len(self._tail) > self._limit:
            del self._tail[: len(self._tail) - self._limit]

    def text(self) -> str:
        return self._tail.decode("utf-8", errors="replace")


class PayloadFailureError(LauncherError):
    """Ожидаемая ошибка операции: payload объявил её и назвал причину.

    Не нарушение контракта: поток отработал штатно, операция сообщила отказ.
    Текст пригоден для показа пользователю и LLM — трейсбека в нём нет.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class ErrorKind:
    """Выводит строковый kind ошибки для показа и истории: у ожидаемых
    отказов (PayloadFailureError, ToolRefusalError) — их собственный kind,
    у остальных — имя класса исключения."""

    @staticmethod
    def of(error: Exception) -> str:
        if isinstance(error, PayloadFailureError):
            return error.kind

        if isinstance(error, ToolRefusalError):
            return error.kind

        return type(error).__name__


class LaunchPayload:
    """Кодирует строки лога процесса песочницы кадрами `sandbox-log:` в
    stderr — так гостевой лог доезжает до журнала приложения через релей
    хоста (SandboxLogRelay)."""

    LOG_MARKER: ClassVar[str] = "sandbox-log:"

    @classmethod
    def encode_log(cls, level: str, name: str, message: str) -> str:
        """Строка лога: многострочное сообщение экранируется в одну строку."""
        body = json.dumps(
            {"lvl": level, "name": name, "msg": message},
            ensure_ascii=False,
        )
        return f"{cls.LOG_MARKER}{body}"


class RowStream:
    """Приводит строки БД-драйверов к JSON-виду и кодирует их в строчный
    поток (NDJSON) для табличных инструментов."""

    _ANY: ClassVar[TypeAdapter[Any]] = TypeAdapter(Any)

    @classmethod
    def plain(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        """Строка драйвера -> JSON-совместимые значения (pydantic'ом).

        Руками декодируются только сырые байты: не-utf8 bytea ронял бы
        pydantic-дамп; остальное (Decimal, UUID, date, set) приводит pydantic.
        """
        decoded = {name: cls._debytes(value) for name, value in row.items()}

        plain = cls._ANY.dump_python(decoded, mode="json")
        if not isinstance(plain, dict):
            msg = f"row must dump to an object, got {type(plain).__name__}"
            raise LauncherError(msg)

        return plain

    @classmethod
    def _debytes(cls, value: Any) -> Any:
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).decode("utf-8", errors="replace")

        if isinstance(value, (list, tuple)):
            return [cls._debytes(item) for item in value]

        if isinstance(value, dict):
            return {name: cls._debytes(item) for name, item in value.items()}

        return value

    @staticmethod
    def encode(row: Mapping[str, Any]) -> str:
        return json.dumps(row, ensure_ascii=False)


class ClippedText(BaseModel):
    """Начало текста в пределах байтового бюджета с пометкой об усечении.

    Ограничивает то, что уходит LLM и в чат из длинного вывода.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    total_bytes: int
    truncated: bool

    ENCODING: ClassVar[str] = "utf-8"
    NOTICE: ClassVar[str] = "\n…[truncated: {kept} of {total} bytes shown]"

    @classmethod
    def of(cls, text: str, max_bytes: int) -> ClippedText:
        if max_bytes <= 0:
            msg = f"max_bytes must be positive, got {max_bytes}"
            raise ValueError(msg)

        raw = text.encode(cls.ENCODING)
        total = len(raw)
        if total <= max_bytes:
            return cls(text=text, total_bytes=total, truncated=False)

        # обрезка по байтам рвёт последний символ — его отбрасываем
        head = raw[:max_bytes].decode(cls.ENCODING, errors="ignore")
        kept = len(head.encode(cls.ENCODING))
        notice = cls.NOTICE.format(kept=kept, total=total)

        return cls(text=f"{head}{notice}", total_bytes=total, truncated=True)


@dataclass(frozen=True)
class RunResult:
    """Процессные поля завершённого запуска: код возврата, вывод,
    длительность, таймаут.

    spawn_ms — сколько занял сам fork/exec; first_output_ms — латентность
    первого байта любого потока от старта, None — процесс не вывел ничего.
    """

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    spawn_ms: int = 0
    first_output_ms: int | None = None


class LaunchOutcome:
    """Итог shell-команды (call_text): процессные поля плюс диагностика
    упёршегося лимита песочницы."""

    def __init__(self, tool: str, result: RunResult, diagnostic: str) -> None:
        self.tool = tool
        self.result = result
        self.diagnostic = diagnostic

    @property
    def succeeded(self) -> bool:
        if self.result.timed_out:
            return False
        return self.result.exit_code == 0


@dataclass(frozen=True)
class ToolOutcome:
    """Итог вызова инструмента: разобранный конверт плюс процессные поля.

    Отдаётся из ToolCall.result(). reply не опционален: если конверта нет,
    result() поднимает LauncherError с хвостом stderr — «итога без ответа»
    как состояния не существует.
    """

    reply: ReplyOk | ReplyError
    run: RunResult
    diagnostic: str


class EnvelopeReply:
    """Разбирает байты канала tool_result в ReplyOk/ReplyError.

    Общая точка обеих реализаций ToolLauncher: пустой канал или битый JSON
    превращаются в LauncherError с диагностикой, а не в молчание.
    """

    @staticmethod
    def parse(
        tool: str, raw: bytes | bytearray, run: RunResult, diagnostic: str
    ) -> ReplyOk | ReplyError:
        if not raw:
            msg = (
                f"{tool}: no envelope on tool_result "
                f"(rc={run.exit_code}, timed_out={run.timed_out}); "
                f"tool_stderr={run.stderr!r}"
            )
            if diagnostic:
                msg = f"{msg}; {diagnostic}"

            raise LauncherError(msg)

        try:
            return REPLY.validate_json(bytes(raw))
        except ValueError as exc:
            msg = f"{tool}: envelope does not match contract: {exc}"
            raise LauncherError(msg) from exc


class ToolCall(Protocol):
    """Протокол одного открытого вызова инструмента: вход кадрами, кадры
    наружу, конверт результата в конце. Реализация — PumpedCall
    (boba.toolkit.pump), создаётся ToolLauncher.open().

    Конфиг команды лончер отправляет телу сам, первым кадром; send добавляет
    прикладные кадры, done_sending закрывает вход кадром eos и EOF. send
    пишет в пайп тела напрямую и блокируется на полном буфере, пока тело не
    прочитает своё, — так скорость входа прижимается к скорости тела; писать
    можно из любого потока, записи атомарны. frames — итератор кадров канала
    tool_frames, один читатель на вызов: он блокирует до следующего кадра и
    кончается вместе с вызовом; result дожидается завершения и разбирает
    конверт. close добивает вызов; выход из контекста зовёт close.
    """

    @abstractmethod
    def send(self, frame: ToolFrame) -> None:
        """Прикладной кадр телу; после done_sending — LauncherError."""
        ...

    @abstractmethod
    def done_sending(self) -> None:
        """Конец входа: телу уходит eos, дальше stdin закрывается."""
        ...

    @abstractmethod
    def frames(self) -> Iterator[ToolFrame]:
        """Кадры тела по мере поступления, до конца вызова."""
        ...

    @abstractmethod
    def result(self) -> ToolOutcome:
        """Дождаться завершения и разобрать конверт; без конверта — LauncherError."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Добить вызов; после result — ничего не делает."""
        ...

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class ToolLauncher(Protocol):
    """Протокол исполнителя инструментов: запуск тела в изолированном
    окружении.

    open() начинает потоковый вызов команды модуля инструментов и отдаёт
    ToolCall; call_text() исполняет shell-команду и возвращает её вывод как
    есть (bash-инструмент). Реализации: ProcessToolCaller (dev-режим без
    песочницы) и ZygoteToolCaller (bwrap-песочница). Накопительный вызов
    строится поверх open компонентом CollectedCall — отдельного входа в
    порт у него нет.
    """

    @abstractmethod
    def open(self, command: ToolCommand) -> ToolCall:
        """Открыть вызов команды модуля инструментов."""
        ...

    @abstractmethod
    def call_text(self, command: str, stdin: str) -> LaunchOutcome:
        """Выполнить команду; stdout/stderr/rc возвращаются без разбора."""
        ...


class CollectedCall:
    """Накопительный вызов поверх потокового: открыть, дочитать кадры в
    никуда, вернуть конверт.

    Так инструменты зовёт LLM: модели не нужны промежуточные кадры, нужен
    итог. Отдельного «одноразового» протокола нет — это просто способ
    прочитать потоковый вызов до конца.
    """

    @staticmethod
    def of(launcher: ToolLauncher, command: ToolCommand) -> ToolOutcome:
        with launcher.open(command) as call:
            call.done_sending()

            for _ in call.frames():
                continue

            return call.result()


class LauncherFactory(Protocol):
    """Протокол фабрики исполнителей: по имени инструмента отдаёт его
    ToolLauncher. Какое окружение достанется инструменту (секция песочницы,
    process-режим), решает приложение при сборке инструментов."""

    @abstractmethod
    def __call__(self, tool: str, /) -> ToolLauncher:
        """Исполнитель для инструмента tool (метка идёт в логи и диагностику)."""
        ...
