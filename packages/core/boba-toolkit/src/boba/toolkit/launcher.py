"""Порт запуска инструмента: контракт между инструментами и исполнителями.

Телу инструмента нужен запуск в отдельном процессе, но знать, как тот
устроен (subprocess, bwrap, cgroup), оно не должно. Здесь объявлены
протоколы ToolLauncher (открыть вызов), ToolCall (один открытый вызов),
CallInputPort (один его вход) и CallGate (барьер группы), модели итога и
общие буферы каналов; реализации — ProcessToolCaller
(boba.toolrun.process) и ZygoteToolCaller (boba.sandbox.zygote) —
подставляются снаружи. Вызов всегда потоковый: вход и выход — кадры
(boba.toolkit.frames); накопительный «вызвал и получил итог» строится
поверх него компонентом CollectedCall.

Ошибки:
LauncherError — исполнитель нарушил контракт, результату доверять нельзя.
PayloadFailureError — инструмент сообщил об ошибке конвертом.
ChannelOverflowError — канал вызова превысил байтовый потолок.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from types import TracebackType
from typing import Any, ClassVar, Protocol, Self

from pydantic import BaseModel, ConfigDict, TypeAdapter

from boba.toolkit.failure import ReportedError
from boba.toolkit.frames import ToolFrame
from boba.toolkit.protocol import REPLY, ReplyError, ReplyOk, ToolCommand
from boba.toolkit.result import FailureResult
from boba.toolkit.stream import Chunk

__all__ = [
    "CallGate",
    "CallInputPort",
    "CappedChannel",
    "ChannelOverflowError",
    "ChannelTail",
    "ClippedText",
    "CollectedCall",
    "EnvelopeReply",
    "FrameSink",
    "FrameTap",
    "LaunchPayload",
    "LauncherError",
    "LauncherFactory",
    "ObservedCall",
    "PayloadFailureError",
    "RowStream",
    "RunResult",
    "TappedCall",
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

        msg = (
            f"channel {self._channel} collected {len(self._data)} bytes, "
            f"the limit is {self._limit} bytes; the call was killed"
        )
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


class PayloadFailureError(LauncherError, ReportedError):
    """Вызов инструмента закончился ошибкой, уже упакованной в результат.

    Не нарушение контракта: поток отработал штатно, а ошибку тела, группы
    или обвязки описывает failure — его и показывают чат, история и журнал.
    """

    def __init__(self, failure: FailureResult) -> None:
        super().__init__(failure.llm_view())
        self._failure = failure

    def failure(self) -> FailureResult:
        return self._failure


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
            msg = (
                f"RowStream.plain: driver row must dump to a JSON object, "
                f"got {type(plain).__name__}"
            )
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
            msg = f"ClippedText max_bytes must be positive, got {max_bytes}"
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
            lines = [
                f"{tool}: the tool process ended without a result "
                f"(rc={run.exit_code}, timed_out={run.timed_out})"
            ]
            if diagnostic:
                lines.append(diagnostic)

            if run.stderr.strip():
                lines.append(f"stderr tail:\n{run.stderr.rstrip()}")

            raise LauncherError("\n".join(lines))

        try:
            return REPLY.validate_json(bytes(raw))
        except ValueError as exc:
            shown = bytes(raw[:200])
            msg = (
                f"{tool}: tool_result envelope {shown!r} does not match "
                f"the reply contract: {exc}"
            )
            raise LauncherError(msg) from exc


class CallInputPort(Protocol):
    """Протокол одного входа открытого вызова: пайп во входной порт тела.

    send и send_bytes пишут в пайп напрямую и блокируются на полном буфере,
    пока тело не прочитает своё, — так скорость входа прижимается к
    скорости тела; писать можно из любого потока, записи атомарны. finish
    даёт телу EOF. take_fd отдаёт дескриптор перекачке (CallRelay): дальше
    входом владеет она, send и finish больше не работают. Реализация —
    FrameInput (boba.toolkit.pump).
    """

    @abstractmethod
    def send(self, frame: ToolFrame) -> None:
        """Кадр телу; после finish, take_fd или разрыва — LauncherError."""
        ...

    @abstractmethod
    def send_bytes(self, data: Chunk) -> None:
        """Голые байты телу — вход сырого порта."""
        ...

    @abstractmethod
    def finish(self) -> None:
        """Конец входа: EOF телу; повтор безвреден."""
        ...

    @abstractmethod
    def take_fd(self) -> int:
        """Отдать дескриптор входа перекачке вместе с владением."""
        ...


class CallGate(Protocol):
    """Протокол барьера открытого вызова на стороне хоста.

    Тело, объявившее StreamGroup, сообщает готовность зафиксировать
    результат и ждёт ответа. В режиме CallGateMode.AUTO хост разрешает
    сразу; в HELD ответ держит владелец — группа вызовов: claim отдаёт ей
    сигнал готовности, release разрешает фиксацию, refuse запрещает.
    Реализация — HostGate (boba.toolkit.pump).
    """

    @abstractmethod
    def claim(self, on_ready: Callable[[], None]) -> None:
        """Отдать сигнал готовности владельцу; пришедший раньше — сразу."""
        ...

    @abstractmethod
    def release(self) -> None:
        """Разрешить телу фиксацию; повтор и ответ после refuse безвредны."""
        ...

    @abstractmethod
    def refuse(self) -> None:
        """Запретить телу фиксацию: его ready() поднимет ошибку."""
        ...


class ToolCall(Protocol):
    """Протокол одного открытого вызова инструмента: входы кадрами, кадры
    наружу, конверт результата в конце. Реализация — PumpedCall
    (boba.toolkit.pump), создаётся ToolLauncher.open().

    Конфиг команды лончер отправляет телу сам, своим каналом. inputs —
    входы вызова в порядке ToolCommand.inputs, у каждого свой пайп;
    done_sending закрывает все, что ещё открыты. frames — итератор кадров
    канала tool_frames, один читатель на вызов: он блокирует до следующего
    кадра и кончается вместе с вызовом; result дожидается завершения и
    разбирает конверт. close добивает вызов; выход из контекста зовёт close.
    """

    @abstractmethod
    def inputs(self) -> Sequence[CallInputPort]:
        """Входы вызова по порядку ToolCommand.inputs."""
        ...

    @abstractmethod
    def done_sending(self) -> None:
        """Конец всех входов: EOF каждому ещё открытому."""
        ...

    @abstractmethod
    def gate(self) -> CallGate:
        """Барьер вызова: ответ телу на StreamGroup.ready()."""
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


@dataclass(frozen=True)
class TappedCall:
    """Вызов-источник для splice-перекачки: сам вызов и дескриптор его
    канала кадров, который хост не разбирает. Возвращается методом
    open_tap; дескриптором владеет перекачка (CallRelay.splice)."""

    call: ToolCall
    frames_fd: int


class ToolLauncher(Protocol):
    """Протокол исполнителя инструментов: запуск тела в изолированном
    окружении.

    open() начинает потоковый вызов команды модуля инструментов и отдаёт
    ToolCall; open_tap() — вариант для перекачки: канал кадров вызова
    отдаётся дескриптором и хостом не разбирается.
    Реализации: ProcessToolCaller (dev-режим без песочницы) и
    ZygoteToolCaller (bwrap-песочница). Накопительный вызов строится поверх
    open компонентом CollectedCall — отдельного входа в порт у него нет.
    """

    @abstractmethod
    def open(self, command: ToolCommand) -> ToolCall:
        """Открыть вызов команды модуля инструментов."""
        ...

    @abstractmethod
    def open_tap(self, command: ToolCommand) -> TappedCall:
        """Открыть вызов-источник перекачки: канал кадров — дескриптором."""
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


class FrameSink(Protocol):
    """Приёмник кадров вызова на хосте: зовётся в потоке чтения кадров по
    одному кадру, порядок — порядок канала."""

    @abstractmethod
    def take(self, frame: ToolFrame) -> None: ...


class ObservedCall:
    """Вызов, кадры которого хост читает сам: открыть, отдать каждый кадр
    приёмнику, вернуть конверт.

    Так хост запускает инструменты, у которых кадры — и есть результат
    (снятие метаданных источника): накопительный путь их выбросил бы.
    """

    @staticmethod
    def of(
        launcher: ToolLauncher, command: ToolCommand, sink: FrameSink
    ) -> ToolOutcome:
        with launcher.open(command) as call:
            call.done_sending()

            for frame in call.frames():
                sink.take(frame)

            return call.result()


class FrameTap:
    """Contextvar-переноска приёмника кадров: вызывающий ставит приёмник перед
    вызовом инструмента, обёртка запуска читает его в потоке тела и ведёт
    вызов через ObservedCall. Без приёмника вызов идёт накопительно."""

    _SINK: ClassVar[ContextVar[FrameSink | None]] = ContextVar(
        "boba_frame_tap", default=None
    )

    @classmethod
    def set(cls, sink: FrameSink) -> Token[FrameSink | None]:
        return cls._SINK.set(sink)

    @classmethod
    def reset(cls, token: Token[FrameSink | None]) -> None:
        cls._SINK.reset(token)

    @classmethod
    def get(cls) -> FrameSink | None:
        return cls._SINK.get()

    @classmethod
    @contextmanager
    def applied(cls, sink: FrameSink) -> Generator[None, None, None]:
        token = cls.set(sink)
        try:
            yield
        finally:
            cls.reset(token)


class LauncherFactory(Protocol):
    """Протокол фабрики исполнителей: по имени инструмента отдаёт его
    ToolLauncher. Какое окружение достанется инструменту (секция песочницы,
    process-режим), решает приложение при сборке инструментов."""

    @abstractmethod
    def __call__(self, tool: str, /) -> ToolLauncher:
        """Исполнитель для инструмента tool (метка идёт в логи и диагностику)."""
        ...
