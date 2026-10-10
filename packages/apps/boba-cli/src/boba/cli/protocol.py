"""Протокол стандартных потоков: строки JSON на входе и выходе процесса.

Устройство — план, разделы 5.8 и 5.18, по образцу `stream-json` Claude Code.
На входе три рода строк: сообщение пользователя (в очередь), команда
`control_request` (исполняется сразу, ответ — `control_response` с тем же
`request_id`), ответ `control_response` на вопрос агента (`can_use_tool`);
`control_cancel_request` и `keep_alive` служебные. На выходе — события ядра
(EventSink) строками: `system/init`, `stream_event`, записи разговора как в
журнале, `control_request`, `control_cancel_request`, `result`,
`system/error`. OutputWriter держит ограниченную очередь строк: когда
читатель вывода встал, ход ждёт на записи, а не копит память. Битая строка,
неизвестный `type` и неизвестная команда дают событие `error`, процесс живёт.

Ошибки наружу не выходят: всё, что пришло с ввода, разбирается в событие
`error` или ответ `control_response` с `subtype: error`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, BinaryIO, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from boba.agent.agent import Agent
from boba.agent.control import AllowAnswer, DenyAnswer, PermissionAnswer
from boba.agent.events import (
    AgentEvent,
    ControlCancelEvent,
    ControlRequestEvent,
    ErrorEvent,
    EventSink,
    InitEvent,
    RecordEvent,
    ResultEvent,
    StreamEvent,
)
from boba.agent.permissions import PermissionModeError
from boba.agent.queue import QueueEntry, QueueKind, QueuePriority, QueueRefusedError
from boba.agent.records import PermissionMode, PermissionRule, RecordCodec
from boba.agent.session import SessionError

logger = logging.getLogger(__name__)

__all__ = [
    "AnswerJson",
    "CommandJson",
    "CommandSubtype",
    "ControlRequestLine",
    "ControlResponseLine",
    "EndOfInput",
    "InboundLine",
    "InputReader",
    "LineDispatcher",
    "LineParseError",
    "LineParser",
    "OutputWriter",
    "UserLine",
]


class InboundType(StrEnum):
    """Виды строк на входе."""

    USER = "user"
    CONTROL_REQUEST = "control_request"
    CONTROL_RESPONSE = "control_response"
    CONTROL_CANCEL = "control_cancel_request"
    KEEP_ALIVE = "keep_alive"


class OutboundType(StrEnum):
    """Виды строк на выходе, которые не являются записями журнала."""

    SYSTEM = "system"
    STREAM_EVENT = "stream_event"
    RESULT = "result"
    CONTROL_REQUEST = "control_request"
    CONTROL_RESPONSE = "control_response"
    CONTROL_CANCEL = "control_cancel_request"


class SystemSubtype(StrEnum):
    INIT = "init"
    ERROR = "error"


class ResponseSubtype(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


class CommandSubtype(StrEnum):
    """Команды запускающей программы первой версии (раздел 5.8)."""

    INITIALIZE = "initialize"
    INTERRUPT = "interrupt"
    SET_PERMISSION_MODE = "set_permission_mode"
    CANCEL_ASYNC_MESSAGE = "cancel_async_message"
    END_SESSION = "end_session"


class WireModel(BaseModel):
    """Основа моделей провода: ключи camelCase и snake_case принимаются оба."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="ignore", frozen=True
    )


class UserMessageJson(WireModel):
    role: Literal["user"] = "user"
    content: str


class UserLine(WireModel):
    """Сообщение пользователя: в очередь ввода с приоритетом из строки."""

    type: Literal[InboundType.USER]
    message: UserMessageJson
    uuid: str | None = None
    priority: QueuePriority | None = None
    kind: QueueKind = QueueKind.PROMPT
    is_meta: bool = False

    def entry(self) -> QueueEntry:
        return QueueEntry(
            kind=self.kind,
            text=self.message.content,
            priority=self.priority,
            is_meta=self.is_meta,
            id=self.uuid,
        )


class CommandJson(WireModel):
    """Тело команды: подвид и его поля. У `initialize` — системный промпт и
    добавка к нему; остальные поля Claude Code (хуки, серверы SDK, агенты)
    агенту не нужны и пропускаются с записью в лог."""

    subtype: CommandSubtype
    system_prompt: str | None = None
    append_system_prompt: str | None = None
    cancel_queued: bool = False
    mode: PermissionMode | None = None
    message_id: str | None = None
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="allow", frozen=True
    )

    def ignored_fields(self) -> Sequence[str]:
        extra = self.model_extra
        if not extra:
            return ()

        return sorted(extra)


class ControlRequestLine(WireModel):
    type: Literal[InboundType.CONTROL_REQUEST]
    request_id: str = Field(min_length=1)
    request: CommandJson


class AnswerBehavior(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class AnswerJson(WireModel):
    """Ответ на `can_use_tool`: разрешение с правками либо отказ с текстом."""

    behavior: AnswerBehavior
    updated_input: Mapping[str, Any] | None = None
    updated_permissions: Sequence[PermissionRule] = ()
    message: str = ""

    def answer(self) -> PermissionAnswer:
        if self.behavior is AnswerBehavior.DENY:
            return DenyAnswer(message=self.message)

        return AllowAnswer(
            updated_input=self.updated_input,
            updated_permissions=list(self.updated_permissions),
        )


class ControlResponseBody(WireModel):
    request_id: str = Field(min_length=1)
    subtype: ResponseSubtype = ResponseSubtype.SUCCESS
    response: AnswerJson | None = None
    error: str | None = None


class ControlResponseLine(WireModel):
    type: Literal[InboundType.CONTROL_RESPONSE]
    response: ControlResponseBody


class ControlCancelLine(WireModel):
    type: Literal[InboundType.CONTROL_CANCEL]
    request_id: str = ""


class KeepAliveLine(WireModel):
    type: Literal[InboundType.KEEP_ALIVE]


InboundLine = Annotated[
    UserLine
    | ControlRequestLine
    | ControlResponseLine
    | ControlCancelLine
    | KeepAliveLine,
    Field(discriminator="type"),
]


class InboundAdapter(BaseModel):
    """Обёртка для разбора одной строки в её модель по полю `type`."""

    line: InboundLine


class LineParseError(Exception):
    """Строка ввода не разбирается в известную модель."""


class LineParser:
    """Строка байтов → модель строки ввода."""

    ENCODING: ClassVar[str] = "utf-8"

    def parse(self, raw: bytes) -> InboundLine:
        try:
            document = json.loads(raw.decode(self.ENCODING))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            msg = f"the input line is not valid JSON: {exc}"
            raise LineParseError(msg) from exc

        if not isinstance(document, Mapping):
            msg = f"the input line must be a JSON object, got {type(document).__name__}"
            raise LineParseError(msg)

        try:
            return InboundAdapter.model_validate({"line": document}).line
        except ValidationError as exc:
            kind = document.get("type")
            msg = f"the input line of type {kind!r} does not match the protocol: {exc}"
            raise LineParseError(msg) from exc


class EventLines:
    """События ядра → строки протокола наружу."""

    def __init__(self, codec: RecordCodec, public_settings: Mapping[str, Any]) -> None:
        self._codec = codec
        self._settings = public_settings

    def line(self, event: AgentEvent) -> str:
        if isinstance(event, RecordEvent):
            return self._codec.line(event.record)

        return self._dumps(self._document(event))

    def response(
        self, request_id: str, payload: Mapping[str, Any] | None, error: str | None
    ) -> str:
        body: dict[str, Any] = {
            "request_id": request_id,
            "subtype": ResponseSubtype.SUCCESS.value,
        }
        if error is not None:
            body["subtype"] = ResponseSubtype.ERROR.value
            body["error"] = error

        if payload is not None:
            body["response"] = dict(payload)

        return self._dumps(
            {"type": OutboundType.CONTROL_RESPONSE.value, "response": body}
        )

    def _document(self, event: AgentEvent) -> dict[str, Any]:
        if isinstance(event, InitEvent):
            document = event.model_dump(mode="json")
            document["type"] = OutboundType.SYSTEM.value
            document["subtype"] = SystemSubtype.INIT.value
            document["settings"] = dict(self._settings)
            return document

        if isinstance(event, StreamEvent):
            return {
                "type": OutboundType.STREAM_EVENT.value,
                "event": event.model_dump(mode="json"),
            }

        if isinstance(event, ResultEvent):
            document = event.model_dump(mode="json")
            document["type"] = OutboundType.RESULT.value
            document["subtype"] = document.pop("outcome")
            return document

        if isinstance(event, ErrorEvent):
            document = event.model_dump(mode="json")
            document["type"] = OutboundType.SYSTEM.value
            document["subtype"] = SystemSubtype.ERROR.value
            return document

        if isinstance(event, ControlRequestEvent):
            return {
                "type": OutboundType.CONTROL_REQUEST.value,
                "request_id": event.request_id,
                "request": event.request.model_dump(mode="json"),
            }

        if isinstance(event, ControlCancelEvent):
            return {
                "type": OutboundType.CONTROL_CANCEL.value,
                "request_id": event.request_id,
            }

        msg = f"Unknown agent event {type(event).__name__}. This is a bug."
        raise TypeError(msg)

    @staticmethod
    def _dumps(document: Mapping[str, Any]) -> str:
        return json.dumps(document, ensure_ascii=False)


class OutputWriter(EventSink):
    """Реализация EventSink: строки протокола в дескриптор вывода через
    ограниченную очередь и поток записи.

    Создаёт вход процесса до сборки агента и отдаёт ядру как приёмник
    событий. Запись идёт в отдельном потоке: заблокированный канал не держит
    цикл событий, а полная очередь держит того, кто пишет.
    """

    QUEUE_SIZE: ClassVar[int] = 256
    ENCODING: ClassVar[str] = "utf-8"

    def __init__(self, fd: int, lines: EventLines) -> None:
        self._fd = fd
        self._lines = lines
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=self.QUEUE_SIZE)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="output-writer")

    async def stop(self) -> None:
        """Дописывает очередь до конца и останавливает поток записи."""
        await self._queue.put(None)
        if self._task is not None:
            await self._task
            self._task = None

    async def emit(self, event: AgentEvent) -> None:
        await self._queue.put(self._lines.line(event))

    async def respond(
        self, request_id: str, payload: Mapping[str, Any] | None, error: str | None
    ) -> None:
        await self._queue.put(self._lines.response(request_id, payload, error))

    async def _run(self) -> None:
        while True:
            line = await self._queue.get()
            if line is None:
                return

            data = (line + "\n").encode(self.ENCODING)
            try:
                await asyncio.to_thread(self._write_all, data)
            except OSError as exc:
                logger.error("output: cannot write a protocol line: %s", exc)
                return

    def _write_all(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(self._fd, view)
            view = view[written:]


class EndOfInput(StrEnum):
    """Почему чтение ввода закончилось."""

    EOF = "eof"
    END_SESSION = "end_session"


class LineDispatcher:
    """Исполняет строки ввода над открытым агентом (раздел 5.8)."""

    OUTPUT_STYLE: ClassVar[str] = "normal"

    def __init__(
        self, agent: Agent, writer: OutputWriter, models: Sequence[str]
    ) -> None:
        self._agent = agent
        self._writer = writer
        self._models = list(models)
        self._parser = LineParser()
        self._ended: EndOfInput | None = None

    @property
    def ended(self) -> EndOfInput | None:
        return self._ended

    async def handle(self, raw: bytes) -> None:
        try:
            line = self._parser.parse(raw)
        except LineParseError as exc:
            await self._writer.emit(ErrorEvent(message=str(exc)))
            return

        if isinstance(line, UserLine):
            await self._user(line)
            return

        if isinstance(line, ControlRequestLine):
            await self._command(line)
            return

        if isinstance(line, ControlResponseLine):
            self._answer(line)
            return

        if isinstance(line, ControlCancelLine):
            logger.info(
                "input: control_cancel_request %s ignored: commands run at once",
                line.request_id,
            )

    def end(self, why: EndOfInput) -> None:
        self._ended = why

    async def _user(self, line: UserLine) -> None:
        try:
            self._agent.queue.enqueue(line.entry())
        except (QueueRefusedError, ValueError) as exc:
            await self._writer.emit(
                ErrorEvent(message=f"the message was refused: {exc}")
            )

    async def _command(self, line: ControlRequestLine) -> None:
        subtype = line.request.subtype
        try:
            payload = self._execute(line.request)
        except (PermissionModeError, SessionError) as exc:
            await self._writer.respond(line.request_id, None, str(exc))
            return

        if subtype is CommandSubtype.END_SESSION:
            self.end(EndOfInput.END_SESSION)

        await self._writer.respond(line.request_id, payload, None)

    def _execute(self, command: CommandJson) -> dict[str, Any]:
        """Ошибки:
        PermissionModeError — режим недоступен при параметрах запуска.
        SessionError — системный промпт меняют посреди хода.
        """
        subtype = command.subtype
        if subtype is CommandSubtype.INITIALIZE:
            return self._initialize(command)

        if subtype is CommandSubtype.INTERRUPT:
            stopped = self._agent.interrupt(cancel_queued=command.cancel_queued)
            return {"stopped": stopped}

        if subtype is CommandSubtype.SET_PERMISSION_MODE:
            if command.mode is None:
                msg = (
                    "set_permission_mode needs a 'mode' field with the permission mode."
                )
                raise PermissionModeError(msg)

            mode = self._agent.set_permission_mode(command.mode)
            return {"mode": mode.value}

        if subtype is CommandSubtype.CANCEL_ASYNC_MESSAGE:
            removed = False
            if command.message_id is not None:
                removed = self._agent.queue.pop_one(command.message_id) is not None

            return {"removed": removed}

        return {}

    def _initialize(self, command: CommandJson) -> dict[str, Any]:
        """Системный промпт и добавка к нему — как у Claude Code, где они
        приходят первой строкой ввода, а не флагами; ответ — возможности
        процесса.

        Ошибки:
        SessionError — ход идёт, промпт менять поздно.
        """
        ignored = command.ignored_fields()
        if ignored:
            logger.info(
                "initialize: fields %s are not supported by this agent and are ignored",
                ", ".join(ignored),
            )

        sections = list(self._agent.system_prompt)
        if command.system_prompt is not None:
            sections = [command.system_prompt]

        if command.append_system_prompt is not None:
            sections.append(command.append_system_prompt)

        if sections != list(self._agent.system_prompt):
            self._agent.set_system_prompt(sections)

        return {
            "commands": [],
            "agents": [],
            "output_style": self.OUTPUT_STYLE,
            "available_output_styles": [self.OUTPUT_STYLE],
            "models": self._models,
            "account": {},
            "pid": os.getpid(),
        }

    def _answer(self, line: ControlResponseLine) -> None:
        body = line.response
        if body.response is None:
            logger.warning(
                "input: control_response %s without a 'response' body is dropped",
                body.request_id,
            )
            return

        taken = self._agent.answer(body.request_id, body.response.answer())
        if not taken:
            logger.warning(
                "input: control_response %s matches no pending question; dropped",
                body.request_id,
            )


class InputReader:
    """Читает строки стандартного ввода и отдаёт их диспетчеру; пустые байты
    означают конец ввода.

    Канал и терминал читаются без потока — через цикл событий, чтобы
    остановка процесса не ждала заблокированного чтения; обычный файл
    (ввод перенаправлен из файла) цикл читать не умеет, он идёт потоком.
    """

    LINE_LIMIT: ClassVar[int] = 64 * 1024 * 1024

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._piped: asyncio.StreamReader | None = None

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=self.LINE_LIMIT)
        try:
            await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), self._stream
            )
        except (ValueError, OSError) as exc:
            logger.info("input: reading stdin in a thread (%s)", exc)
            return

        self._piped = reader

    async def readline(self) -> bytes:
        """Следующая непустая строка; пустые байты — конец ввода."""
        while True:
            raw = await self._raw_line()
            if not raw:
                return raw

            if raw.strip():
                return raw

    async def _raw_line(self) -> bytes:
        if self._piped is not None:
            return await self._piped.readline()

        return await asyncio.to_thread(self._stream.readline)

    async def run(self, dispatcher: LineDispatcher) -> None:
        while dispatcher.ended is None:
            raw = await self.readline()
            if not raw:
                dispatcher.end(EndOfInput.EOF)
                return

            await dispatcher.handle(raw)
