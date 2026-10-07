"""Декларативные порты потокового инструмента: Inbound, Outbound, StreamSpec.

Тело инструмента объявляет каналы данных прямо в подписи —
`feed: Annotated[Inbound[AudioChunk], Injected]` для входа и
`out: Annotated[Outbound[Transcript], Injected]` для выхода. Модель
заголовка несёт свой kind литералом (`kind: Literal["audio.pcm"]`), союз
моделей разбирается дискриминатором pydantic, и каждый кадр валидируется
один раз на границе — тело работает с типизированными Framed, а не с
сырыми байтами заголовков. Входной порт может быть списком —
`feeds: Annotated[Sequence[Inbound[Chunk]], Injected]`: тело получает
все входы разом, у каждого свой канал, и читает их в каком хочет порядке.
Выходы симметричны: портов Outbound в подписи сколько угодно, одиночных и
списками `Sequence[Outbound[...]]`; сколько каналов у списка, решает
вызывающий, тело пишет в каждый своё.

Кроме модельных портов есть истинно сырые — RawInbound и RawOutbound, и их
наследники в boba.toolkit.arrow с разбором потока Arrow IPC:
никаких структур и кадрирования, по каналу идут только сами байты
(passthrough pg->pg, файлы, PCM). Сырой канал совместим только с сырым:
модельный поток кадрирован, и его рамки попали бы в данные.

Декларация — единственный источник правды о каналах инструмента: по ней
ToolMain строит порты для вызова, хост выводит режим каналов (raw-флаги
ToolCommand), а StreamSpec.of_schema отдаёт интроспекцию для манифеста и
проверки стыковки цепочек. Транспортом портам служит ToolIo
(boba.toolkit.frames) — наружу он больше не показывается.

Тело, которое фиксирует результат (коммит, запись таблицы), объявляет барьер
группы — `group: Annotated[StreamGroup, Injected]` — и зовёт
`await group.ready()` перед фиксацией: вызов возвращается, только когда
все связанные каналами вызовы дошли до своего барьера или успешно
закончились.

Тело, которому есть что сказать о ходе работы, объявляет
`progress: Annotated[ToolProgress, Injected]` и зовёт `progress.report(...)`:
отчёты идут хосту своим каналом, мимо stdout.

Ошибки:
StreamGroupAbortedError — группа сорвалась, пока тело ждало барьера:
    фиксировать результат нельзя.
PortDeclarationError — объявление порта нарушено: тип не модель заголовка
    либо kind не Literal-строка.
FrameProtocolError — заголовок пришедшего кадра не подходит объявленной
    модели порта; поднимается у читателя Inbound.
"""

from __future__ import annotations

import asyncio
import io
import os
import threading
import time
from collections.abc import AsyncIterable, AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import UnionType
from typing import (
    Annotated,
    Any,
    ClassVar,
    Generic,
    Literal,
    TypeVar,
    Union,
    get_args,
    get_origin,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    TypeAdapter,
    ValidationError,
)
from pydantic_core import CoreSchema, core_schema

from boba.toolkit.frames import FrameProtocolError, ToolIo
from boba.toolkit.stream import Chunk

__all__ = [
    "ArrowInbound",
    "ArrowOutbound",
    "ArrowStreamError",
    "DownloadProgress",
    "Framed",
    "GateSignal",
    "Inbound",
    "Outbound",
    "PortDecl",
    "PortDeclarationError",
    "PortDirection",
    "ProgressReport",
    "RawInbound",
    "RawOutbound",
    "RawWriter",
    "StageProgress",
    "StreamGroup",
    "StreamGroupAbortedError",
    "StreamPorts",
    "StreamSpec",
    "StreamSpecs",
    "ToolProgress",
]

HeadT = TypeVar("HeadT", bound=BaseModel)


class PortDeclarationError(Exception):
    """Подпись инструмента объявляет порт с нарушением контракта."""


class StreamGroupAbortedError(Exception):
    """Группа связанных вызовов сорвалась: результат фиксировать нельзя."""


class GateSignal(StrEnum):
    """Байты барьера группы: тело сообщает готовность, хост отвечает."""

    READY = "r"
    GO = "g"
    ABORT = "a"

    def bytes(self) -> bytes:
        return self.value.encode("ascii")

    @classmethod
    def of_byte(cls, raw: bytes) -> GateSignal:
        """Ответ хоста из прочитанного байта; пусто (EOF) — ABORT."""
        if not raw:
            return cls.ABORT

        return cls(raw.decode("ascii"))


class StreamGroup:
    """Барьер группы связанных вызовов на стороне тела.

    Вызовы, которые модель связала каналами в одном workflow, живут группой:
    либо фиксируют результат все, либо никто. Тело объявляет барьер в
    подписи и зовёт ready() перед фиксацией — сообщение хосту идёт пайпом
    gate, ответ приходит пайпом verdict. Хост отвечает GO, когда каждый
    вызов группы дошёл до барьера или успешно закончился, и ABORT, если
    кто-то сорвался. Вне группы (запуск человеком, одиночный вызов) ready()
    возвращается сразу. Строится в ToolMain из номеров --fd-gate и
    --fd-verdict.
    """

    def __init__(self, gate_fd: int, verdict_fd: int) -> None:
        self._gate_fd = gate_fd
        self._verdict_fd = verdict_fd

    async def ready(self) -> None:
        """Дождаться решения группы; срыв — StreamGroupAbortedError."""
        await asyncio.to_thread(self._wait)

    def _wait(self) -> None:
        if self._gate_fd < 0:
            return

        os.write(self._gate_fd, GateSignal.READY.bytes())
        answer = GateSignal.of_byte(os.read(self._verdict_fd, 1))

        if answer is GateSignal.GO:
            return

        msg = (
            "stream group aborted at the commit barrier: another call of the "
            "group failed, this call must not commit its result"
        )
        raise StreamGroupAbortedError(msg)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.is_instance_schema(cls)


class PortDirection(StrEnum):
    """Направление порта: данные в тело либо из тела."""

    INBOUND = "in"
    OUTBOUND = "out"


@dataclass(frozen=True)
class Framed(Generic[HeadT]):
    """Один принятый кадр: заголовок уже разобран в модель порта, тело —
    сырые байты (PCM, файл, текст).

    body — memoryview на собственный буфер кадра (одна копия из ядра, без
    пересборок); view владеет буфером, держать его можно сколько угодно.
    Для склейки с bytes используйте bytes(item.body).
    """

    head: HeadT
    body: Chunk


class Inbound(Generic[HeadT]):
    """Входной порт инструмента: итератор типизированных кадров до EOF.

    Тело объявляет его в подписи (`feed: Annotated[Inbound[Chunk], Injected]`)
    и просто итерируется; каждый заголовок валидируется здесь, на границе,
    против объявленной модели — битый kind поднимает FrameProtocolError у
    читателя, а не расползается по телу. Строится в ToolMain поверх ToolIo.
    """

    def __init__(self, io: ToolIo, heads: TypeAdapter[HeadT]) -> None:
        self._io = io
        self._heads = heads

    def __iter__(self) -> Iterator[Framed[HeadT]]:
        for header, body in self._io.read_frames():
            yield Framed(head=self._head_of(header), body=body)

    def _head_of(self, header: bytes) -> HeadT:
        try:
            return self._heads.validate_json(header)
        except ValidationError as exc:
            shown = header[:200]
            msg = (
                f"inbound frame header {shown!r} does not match the declared "
                f"port head models: {exc}"
            )
            raise FrameProtocolError(msg) from exc

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.is_instance_schema(cls)


class Outbound(Generic[HeadT]):
    """Выходной порт инструмента: emit шлёт кадр с типизированным заголовком.

    Тело объявляет его в подписи (`out: Annotated[Outbound[Reply], Injected]`)
    и зовёт emit(head, body); заголовок сериализуется моделью, тело едет
    байтами как есть. Строится в ToolMain поверх ToolIo.
    """

    def __init__(self, io: ToolIo) -> None:
        self._io = io

    def emit(self, head: HeadT, body: Chunk = b"") -> None:
        self._io.emit(head, body)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.is_instance_schema(cls)


ChunkBytes = Annotated[
    int,
    Field(
        default=262144,
        ge=4096,
        le=67108864,
        description=(
            "Размер порции байтов между узлом и трубой: крупнее — меньше "
            "системных вызовов на больших выгрузках, мельче — раньше первые "
            "данные у приёмника. По умолчанию 256 КиБ."
        ),
    ),
]
"""LLM-аргумент сырых насосов: размер порции потока."""


class RawInbound(io.RawIOBase):
    """Истинно сырой входной порт: двоичный файл на чтение поверх провода.

    Никакого кадрирования, моделей и валидации — по каналу идут только сами
    данные (CSV из COPY, файл, Arrow IPC), и тело читает их как файл. Точка
    чтения одна — readinto: очередные байты провода кладутся в буфер
    вызывающего, сколько пришло за одно чтение, 0 — EOF. Буфер и его
    политику выбирает потребитель: chunks и blocks выделяют буфер на порцию
    (blocks — для async-тела: чтение трубы блокирующее, поэтому порция
    берётся в потоке, а цикл событий остаётся свободен); читатель, которому
    нужен read с дочитыванием до размера, ставит поверх порта
    io.BufferedReader со своим буфером. Границы порций произвольны: это
    байтовый поток, а не сообщения. Совместим только с таким же сырым выходом
    (ChainCheck). Строится в ToolMain поверх ToolIo.
    """

    def __init__(self, io_: ToolIo) -> None:
        super().__init__()
        self._io = io_

    def readable(self) -> bool:
        return True

    # read здесь не переопределяется намеренно: бюджет и переиспользование
    # буфера — дело потребителя (io.BufferedReader у pyarrow, свой bytearray у
    # chunks); ядро читает только в чужой буфер
    def readinto(self, buffer: Any) -> int:
        return self._io.read_into(memoryview(buffer).cast("B"))

    def chunks(self, chunk_bytes: int) -> Iterator[memoryview]:
        """Порции не длиннее chunk_bytes до EOF: каждая — свой буфер, заполненный
        через readinto, без промежуточной копии."""
        while True:
            buffer = bytearray(chunk_bytes)
            filled = self.readinto(buffer)
            if filled == 0:
                return

            yield memoryview(buffer)[:filled]

    async def blocks(self, chunk_bytes: int) -> AsyncIterator[memoryview]:
        chunks = self.chunks(chunk_bytes)
        while True:
            chunk = await asyncio.to_thread(next, chunks, None)
            if chunk is None:
                return

            yield chunk

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.is_instance_schema(cls)


class RawOutbound:
    """Истинно сырой выходной порт: байты уходят в провод как есть.

    Никакого кадрирования и преобразований — pg->pg перекачка везёт ровно
    те байты, что отдал COPY. Плата за это — отсутствие метаданных и
    журнала содержимого: канал предназначен для перекачки (splice), хост в
    него не заглядывает. Точка записи одна — write: view вызывающего уходит
    в провод без копии. Запись в трубу блокирующая, пока хост не вычитает
    её, поэтому async-тело зовёт send: та же запись в потоке, цикл событий
    остаётся свободен. Писателю, которому нужен файл (pyarrow,
    io.BufferedWriter), порт отдаёт его методом writer. Строится в ToolMain
    поверх ToolIo.
    """

    def __init__(self, io_: ToolIo) -> None:
        self._io = io_

    def write(self, chunk: memoryview) -> None:
        self._io.write_chunk(chunk)

    async def send(self, chunk: memoryview) -> None:
        await asyncio.to_thread(self.write, chunk)

    def writer(self) -> RawWriter:
        return RawWriter(self)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.is_instance_schema(cls)


class RawWriter(io.RawIOBase):
    """Файл на запись поверх сырого выходного порта: для писателей, которые
    ждут файл и передают в write любой объект с протоколом буфера (bytes,
    pyarrow.Buffer). Здесь он один раз приводится к memoryview без копии —
    порт принимает только его. Создаётся методом RawOutbound.writer."""

    def __init__(self, port: RawOutbound) -> None:
        super().__init__()
        self._port = port

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        view = memoryview(data)
        self._port.write(view)

        return view.nbytes


class ArrowStreamError(Exception):
    """Байты порта не читаются как поток Arrow IPC или оборвались посреди
    пачки."""


class ArrowInbound(RawInbound):
    """Входной порт потока Arrow IPC. На проводе обычный поток IPC (схема,
    пачки, конец), поэтому порт стыкуется с любым сырым концом, который
    пишет Arrow IPC: ClickHouse с FORMAT ArrowStream, ora_stream_out. Сам порт
    сырой, как RawInbound; пачки из него читает ArrowIpc из
    boba.toolkit.arrow — тот тянет pyarrow, которого у хоста, читающего
    объявления инструментов, нет."""


class ArrowOutbound(RawOutbound):
    """Выходной порт потока Arrow IPC: сырой, как RawOutbound; схему и пачки
    пишет ArrowIpc из boba.toolkit.arrow в файл порта (writer)."""


class ProgressReport(BaseModel):
    """Отчёт тела о ходе работы — строка канала ToolChannel.PROGRESS.

    done — сколько сделано в единицах самого инструмента (строки, байты,
    страницы, номер стадии); total — сколько всего, None — итог неизвестен;
    message — короткий текст для человека. Тело пишет отчёт (ToolProgress),
    хост читает его этой же моделью.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    done: float = Field(ge=0)
    total: float | None
    message: str


class ToolProgress:
    """Отчёты тела инструмента о ходе работы.

    Тело объявляет объект в подписи и зовёт report(): хост превращает
    отчёты в уведомления о прогрессе вызова, и пользователь видит, что
    происходит, пока инструмент работает. Строится в ToolMain из номера
    --fd-progress; при запуске человеком отвязан, и report() ничего не
    делает. done обязан расти: отчёт, в котором он не вырос, не уходит —
    этого требует получатель (прогресс вызова только растёт). Отчёты чаще
    INTERVAL_SEC не шлются: последний из пропущенных уходит следующим
    вызовом report() либо в close(), который ToolMain зовёт после тела.
    report() можно звать из любого потока.
    """

    INTERVAL_SEC: ClassVar[float] = 0.5
    MESSAGE_CHARS: ClassVar[int] = 1000
    """Строка отчёта короче PIPE_BUF: запись в пайп атомарна."""
    ENCODING: ClassVar[str] = "utf-8"

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._lock = threading.Lock()
        self._sent_done = -1.0
        self._sent_at = 0.0
        self._held: tuple[float, float | None, str] | None = None

    def report(self, done: float, total: float | None, message: str) -> None:
        """Сделано done из total (None — итог неизвестен); message — что
        сейчас происходит."""
        if self._fd < 0:
            return

        with self._lock:
            if done <= self._sent_done:
                return

            if time.monotonic() - self._sent_at < self.INTERVAL_SEC:
                self._held = (done, total, message)
                return

            self._send(done, total, message)

    def close(self) -> None:
        """Дослать отчёт, придержанный частотой: последний отчёт тела
        обычно итоговый."""
        with self._lock:
            held = self._held
            if held is None:
                return

            self._send(*held)

    def _send(self, done: float, total: float | None, message: str) -> None:
        report = ProgressReport(
            done=done, total=total, message=message[: self.MESSAGE_CHARS]
        )
        self._held = None
        self._sent_done = done
        self._sent_at = time.monotonic()
        line = f"{report.model_dump_json()}\n"
        os.write(self._fd, line.encode(self.ENCODING))

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.is_instance_schema(cls)


class StageProgress:
    """Ход работы стадиями: каждая стадия — следующий отчёт с её номером.

    Создаётся телом инструмента, у которого нет меры работы в строках или
    байтах, но есть последовательность шагов (подключение, курсор,
    выполнение запроса, уведомление сервера базы). total — сколько стадий
    всего; None — заранее неизвестно.
    """

    def __init__(self, progress: ToolProgress, total: int | None) -> None:
        self._progress = progress
        self._total = total
        self._done = 0

    def next(self, message: str) -> None:
        """Началась следующая стадия."""
        self._done += 1
        self._progress.report(self._done, self._total, message)


class DownloadProgress:
    """Ход скачивания отчётами тела инструмента: сколько байт тела получено.

    Создаётся телом, которое качает страницу или файл. counted() отдаёт те
    же порции потока, считая их; total — размер тела, каким его назвал
    сервер в заголовке ответа, None — сервер размера не назвал.
    """

    MIB: ClassVar[int] = 1 << 20

    def __init__(self, progress: ToolProgress, total: int | None) -> None:
        self._progress = progress
        self._total = total

    async def counted(self, chunks: AsyncIterable[bytes]) -> AsyncIterator[bytes]:
        done = 0
        async for chunk in chunks:
            done += len(chunk)
            self._progress.report(
                done, self._total, f"downloaded {done / self.MIB:.1f} MiB"
            )
            yield chunk


class StreamPorts:
    """Разбор портов из подписи инструмента и постройка их для вызова.

    Общая точка хоста и гостя: ToolArgv по is_port исключает порты из argv
    и конфига, ToolMain через build подставляет их в kwargs тела,
    StreamSpec через kinds_of собирает интроспекцию.
    """

    KIND_FIELD: ClassVar[str] = "kind"

    @staticmethod
    def is_group(annotation: Any) -> bool:
        """Параметр — барьер группы StreamGroup: строит гость, как порт."""
        return annotation is StreamGroup

    @staticmethod
    def is_progress(annotation: Any) -> bool:
        """Параметр — отчёты о ходе работы ToolProgress: строит гость."""
        return annotation is ToolProgress

    @classmethod
    def is_port(cls, annotation: Any) -> bool:
        """Параметр — порт либо список портов: значение строит гость, хост его
        не сериализует."""
        if cls.is_many(annotation):
            return True

        return cls._is_single(annotation)

    @classmethod
    def is_many(cls, annotation: Any) -> bool:
        """Список портов Sequence[Inbound[...]]: сколько в нём входов, решает
        вызывающий, у каждого входа свой канал."""
        if get_origin(annotation) is not Sequence:
            return False

        arguments = get_args(annotation)
        if len(arguments) != 1:
            return False

        return cls._is_single(arguments[0])

    @classmethod
    def element_of(cls, annotation: Any) -> Any:
        """Аннотация одного порта: элемент списка либо сам порт."""
        if cls.is_many(annotation):
            return get_args(annotation)[0]

        return annotation

    @classmethod
    def _is_single(cls, annotation: Any) -> bool:
        if cls.is_raw(annotation):
            return True

        return get_origin(annotation) in (Inbound, Outbound)

    @classmethod
    def is_raw(cls, annotation: Any) -> bool:
        """Порт сырого потока (в том числе наследники вроде Arrow-портов):
        структур и валидации заголовков нет."""
        if not isinstance(annotation, type):
            return False

        return issubclass(annotation, RawInbound | RawOutbound)

    @classmethod
    def direction_of(cls, annotation: Any) -> PortDirection:
        if cls.is_raw(annotation) and issubclass(annotation, RawInbound):
            return PortDirection.INBOUND

        if cls.is_raw(annotation):
            return PortDirection.OUTBOUND

        origin = get_origin(annotation)
        if origin is Inbound:
            return PortDirection.INBOUND

        if origin is Outbound:
            return PortDirection.OUTBOUND

        msg = (
            f"expected a port annotation Inbound[...] or Outbound[...], "
            f"got {annotation!r}"
        )
        raise PortDeclarationError(msg)

    @classmethod
    def build(
        cls, annotation: Any, io: ToolIo
    ) -> Inbound[Any] | Outbound[Any] | RawInbound | RawOutbound:
        """Порт для вызова над транспортом ToolIo; сырой порт строится своим
        классом, в том числе наследник."""
        if cls.is_raw(annotation):
            return annotation(io)

        direction = cls.direction_of(annotation)

        if direction is PortDirection.OUTBOUND:
            return Outbound(io)

        return Inbound(io, cls.head_adapter(annotation))

    @classmethod
    def head_adapter(cls, annotation: Any) -> TypeAdapter[Any]:
        """Валидатор заголовков порта: союз моделей — по дискриминатору kind."""
        members = cls._members_of(annotation)

        if len(members) == 1:
            return TypeAdapter(members[0])

        union = members[0]
        for member in members[1:]:
            union = union | member

        discriminated = Annotated[union, Field(discriminator=cls.KIND_FIELD)]
        return TypeAdapter(discriminated)

    @classmethod
    def kinds_of(cls, annotation: Any) -> tuple[str, ...]:
        """Kind'ы кадров порта в порядке объявления моделей."""
        kinds: list[str] = []
        for member in cls._members_of(annotation):
            kinds.append(cls._kind_of(member))

        return tuple(kinds)

    @classmethod
    def _members_of(cls, annotation: Any) -> tuple[type[BaseModel], ...]:
        """Модели заголовков порта: одиночная либо члены союза."""
        arguments = get_args(annotation)
        if len(arguments) != 1:
            msg = (
                f"port {annotation!r} must declare exactly one head model "
                f"type argument, got {len(arguments)}"
            )
            raise PortDeclarationError(msg)

        head = arguments[0]

        candidates: tuple[Any, ...] = (head,)
        if get_origin(head) in (Union, UnionType):
            candidates = get_args(head)

        members: list[type[BaseModel]] = []
        for candidate in candidates:
            if not isinstance(candidate, type):
                msg = (
                    f"port {annotation!r}: head must be a pydantic model class, "
                    f"got {candidate!r}"
                )
                raise PortDeclarationError(msg)

            if not issubclass(candidate, BaseModel):
                msg = (
                    f"port {annotation!r}: head must subclass pydantic BaseModel, "
                    f"got {candidate.__name__}"
                )
                raise PortDeclarationError(msg)

            members.append(candidate)

        return tuple(members)

    @classmethod
    def _kind_of(cls, member: type[BaseModel]) -> str:
        """Kind модели заголовка: Literal-строка поля kind."""
        field = member.model_fields.get(cls.KIND_FIELD)
        if field is None:
            listed = ", ".join(member.model_fields)
            msg = (
                f"head model {member.__name__} has no {cls.KIND_FIELD!r} field "
                f"(fields: {listed}); a port head needs kind: Literal['...']"
            )
            raise PortDeclarationError(msg)

        if get_origin(field.annotation) is not Literal:
            msg = (
                f"head model {member.__name__} must declare "
                f"{cls.KIND_FIELD}: Literal[...] for the port, "
                f"got {field.annotation!r}"
            )
            raise PortDeclarationError(msg)

        values = get_args(field.annotation)
        if len(values) != 1 or not isinstance(values[0], str):
            msg = (
                f"head model {member.__name__} must declare exactly one "
                f"string kind, got {values!r}"
            )
            raise PortDeclarationError(msg)

        return values[0]


class PortDecl(BaseModel):
    """Декларация одного порта для интроспекции: имя параметра, направление,
    kind'ы кадров и список ли это; raw-порт структур не объявляет — kinds пуст.
    many — список Sequence[Inbound[...]] либо Sequence[Outbound[...]]:
    каналов у него столько, сколько назвал вызывающий."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    direction: PortDirection
    kinds: tuple[str, ...]
    raw: bool
    many: bool
    omnivore: bool = False
    """Всеядный вход: стыкуется с любым выходом, сырым и кадровым. Из
    подписи не выводится — такой порт объявляет встроенный узел хоста
    (dev_null), который в байты канала не заглядывает."""


class StreamSpec(BaseModel):
    """Потоковая декларация инструмента, выведенная из его подписи.

    По ней хост узнаёт, какие kind'ы тул принимает и отдаёт, — источник для
    манифеста инструментов и проверки стыковки каналов. Портов в обе
    стороны сколько угодно, каждый одиночный или список; у каждого канала
    свой пайп, а раздачу канала нескольким читателям делает хост.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ports: tuple[PortDecl, ...] = ()
    gated: bool = False
    """Тело объявило барьер группы StreamGroup."""

    @classmethod
    def of_schema(cls, schema: type[BaseModel]) -> StreamSpec:
        """Декларация из args_schema инструмента; без портов — пустая."""
        declared: list[PortDecl] = []

        gated = False
        for name, field in schema.model_fields.items():
            if StreamPorts.is_group(field.annotation):
                gated = True
                continue

            if not StreamPorts.is_port(field.annotation):
                continue

            declared.append(cls._decl_of(name, field.annotation))

        return cls(ports=tuple(declared), gated=gated)

    @staticmethod
    def _decl_of(name: str, annotation: Any) -> PortDecl:
        element = StreamPorts.element_of(annotation)
        many = StreamPorts.is_many(annotation)
        direction = StreamPorts.direction_of(element)
        raw = StreamPorts.is_raw(element)

        kinds: tuple[str, ...] = ()
        if not raw:
            kinds = StreamPorts.kinds_of(element)

        return PortDecl(name=name, direction=direction, kinds=kinds, raw=raw, many=many)

    def streaming(self) -> bool:
        """Инструмент объявил хотя бы один канал данных."""
        return bool(self.ports)

    def outbound(self) -> tuple[PortDecl, ...]:
        """Выходные порты в порядке подписи; пусто — данных наружу нет."""
        ports: list[PortDecl] = []
        for port in self.ports:
            if port.direction is PortDirection.OUTBOUND:
                ports.append(port)

        return tuple(ports)

    def inbound(self) -> tuple[PortDecl, ...]:
        """Входные порты в порядке подписи."""
        ports: list[PortDecl] = []
        for port in self.ports:
            if port.direction is PortDirection.INBOUND:
                ports.append(port)

        return tuple(ports)

    def port(self, name: str) -> PortDecl:
        """Порт по имени параметра; незнакомое имя — PortDeclarationError."""
        for port in self.ports:
            if port.name == name:
                return port

        declared: list[str] = []
        for port in self.ports:
            declared.append(port.name)

        msg = f"no port {name!r} among the declared ports {declared}"
        raise PortDeclarationError(msg)


class StreamSpecs:
    """Потоковые декларации собранных инструментов по именам.

    Значение без состояния процесса: его отдаёт обёртка запуска
    (ToolProcessWrap.guard_all) — в этот момент схема инструмента ещё полная,
    с портами; позже порты из видимой схемы снимаются. Загрузчик складывает
    декларации плагинов в одно значение и кладёт его в реестр инструментов;
    читают цепочка обвязок (поля каналов) и планировщик DAG.
    """

    def __init__(self, declared: Mapping[str, StreamSpec]) -> None:
        self._declared = dict(declared)

    def of(self, name: str) -> StreamSpec:
        """Декларация инструмента; неизвестное имя — пустая (не потоковый)."""
        spec = self._declared.get(name)
        if spec is None:
            return StreamSpec()

        return spec

    def merged(self, other: StreamSpecs) -> StreamSpecs:
        """Эти декларации вместе с other; совпавшее имя берётся из other."""
        return StreamSpecs({**self._declared, **other._declared})

    def declaring(self, name: str, spec: StreamSpec) -> StreamSpecs:
        """Эти декларации и ещё одна — инструмента name."""
        return StreamSpecs({**self._declared, name: spec})
