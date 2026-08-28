"""Сообщения и команды шины: неизменяемые модели с дискриминатором kind, в которых
события хода, запуска, журнала и канваса покидают исполнителя.

Ошибки: своих не выпускает; негодные данные отвергает pydantic.ValidationError.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from boba.messaging.payloads import PayloadRef

__all__ = [
    "AnswerClosed",
    "AnswerInterrupted",
    "AnswerToken",
    "AnyCommand",
    "AnyMessage",
    "CanvasChanged",
    "Command",
    "CommandKind",
    "LockLost",
    "Message",
    "MessageKind",
    "ModelAnswered",
    "Notice",
    "NoticeLevel",
    "RunFinished",
    "RunStateChanged",
    "SignInRefreshRequested",
    "StageEnded",
    "StageQueries",
    "StageStarted",
    "StopRequested",
    "StreamAppended",
    "ThinkingClosed",
    "ThinkingComplete",
    "ThinkingToken",
    "ToolFailed",
    "ToolFinished",
    "ToolStarted",
    "ToolStopped",
    "TurnFinished",
    "TurnOutcome",
    "TurnStarted",
]


class MessageKind(StrEnum):
    """Виды сообщений шины; значение хранится в live_events.kind и в теле конверта."""

    TURN_STARTED = "turn_started"
    MODEL_ANSWERED = "model_answered"
    ANSWER_TOKEN = "answer_token"  # noqa: S105
    ANSWER_CLOSED = "answer_closed"
    ANSWER_INTERRUPTED = "answer_interrupted"
    THINKING_TOKEN = "thinking_token"  # noqa: S105
    THINKING_COMPLETE = "thinking_complete"
    THINKING_CLOSED = "thinking_closed"
    STAGE_STARTED = "stage_started"
    STAGE_QUERIES = "stage_queries"
    STAGE_ENDED = "stage_ended"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    TOOL_FAILED = "tool_failed"
    TOOL_STOPPED = "tool_stopped"
    TURN_FINISHED = "turn_finished"
    RUN_STATE_CHANGED = "run_state_changed"
    RUN_FINISHED = "run_finished"
    STREAM_APPENDED = "stream_appended"
    CANVAS_CHANGED = "canvas_changed"
    SIGNIN_REFRESH_REQUESTED = "signin_refresh_requested"
    NOTICE = "notice"
    LOCK_LOST = "lock_lost"


class CommandKind(StrEnum):
    """Виды команд шины; значение хранится в live_commands.action."""

    STOP = "stop"


class TurnOutcome(StrEnum):
    """Чем закончился ход чата: успехом, остановкой или сбоем."""

    OK = "ok"
    STOPPED = "stopped"
    FAILED = "failed"


class NoticeLevel(StrEnum):
    """Важность уведомления пользователю; интерфейс выбирает по ней способ показа."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Message(BaseModel):
    """Базовый класс сообщения шины."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: MessageKind


class Command(BaseModel):
    """Базовый класс команды области: просьба к держателю что-то сделать, а не факт о
    том, что произошло.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: CommandKind


class TurnStarted(Message):
    """Ход turn_id начался; key — id шага вопроса пользователя, к которому крепится
    ответ.
    """

    kind: Literal[MessageKind.TURN_STARTED] = MessageKind.TURN_STARTED
    turn_id: str = Field(min_length=1)
    key: str = Field(min_length=1)


class ModelAnswered(Message):
    """Модель прислала первый кусок ответа на ход turn_id: ожидание закончилось."""

    kind: Literal[MessageKind.MODEL_ANSWERED] = MessageKind.MODEL_ANSWERED
    turn_id: str = Field(min_length=1)


class AnswerToken(Message):
    """Кусок текста ответа для шага key; получатель дописывает его в конец шага."""

    kind: Literal[MessageKind.ANSWER_TOKEN] = MessageKind.ANSWER_TOKEN
    turn_id: str = Field(min_length=1)
    key: str = Field(min_length=1)
    token: str


class AnswerClosed(Message):
    """Ответ шага key закончен; накопленный текст фиксируется как есть."""

    kind: Literal[MessageKind.ANSWER_CLOSED] = MessageKind.ANSWER_CLOSED
    turn_id: str = Field(min_length=1)
    key: str = Field(min_length=1)


class AnswerInterrupted(Message):
    """Ответ шага key оборван остановкой; note — пометка, которую получатель дописывает
    к тексту.
    """

    kind: Literal[MessageKind.ANSWER_INTERRUPTED] = MessageKind.ANSWER_INTERRUPTED
    turn_id: str = Field(min_length=1)
    key: str = Field(min_length=1)
    note: str = Field(min_length=1)


class ThinkingToken(Message):
    """Кусок рассуждения модели для шага key."""

    kind: Literal[MessageKind.THINKING_TOKEN] = MessageKind.THINKING_TOKEN
    turn_id: str = Field(min_length=1)
    key: str = Field(min_length=1)
    token: str


class ThinkingComplete(Message):
    """Рассуждение шага key пришло целиком без стрима; текст лежит в PayloadStore по
    ссылке text.
    """

    kind: Literal[MessageKind.THINKING_COMPLETE] = MessageKind.THINKING_COMPLETE
    turn_id: str = Field(min_length=1)
    key: str = Field(min_length=1)
    text: PayloadRef


class ThinkingClosed(Message):
    """Открытое рассуждение хода turn_id закончено."""

    kind: Literal[MessageKind.THINKING_CLOSED] = MessageKind.THINKING_CLOSED
    turn_id: str = Field(min_length=1)


class StageStarted(Message):
    """Началась стадия хода (поиск, переформулировка): name — её имя, phase — подпись
    текущей фазы.
    """

    kind: Literal[MessageKind.STAGE_STARTED] = MessageKind.STAGE_STARTED
    turn_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    phase: str


class StageQueries(Message):
    """Стадия name сообщила запросы, которые она выполняет."""

    kind: Literal[MessageKind.STAGE_QUERIES] = MessageKind.STAGE_QUERIES
    turn_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    queries: tuple[str, ...]


class StageEnded(Message):
    """Стадия name закончилась за elapsed_ms миллисекунд; queries — запросы, с которыми
    она работала.
    """

    kind: Literal[MessageKind.STAGE_ENDED] = MessageKind.STAGE_ENDED
    turn_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    queries: tuple[str, ...]
    elapsed_ms: int = Field(ge=0)


class ToolStarted(Message):
    """Вызов инструмента name начался под call_id; аргументы лежат в PayloadStore по
    ссылке args.
    """

    kind: Literal[MessageKind.TOOL_STARTED] = MessageKind.TOOL_STARTED
    turn_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    args: PayloadRef


class ToolFinished(Message):
    """Вызов call_id закончился; результат (артефакт или сырой вывод) лежит в
    PayloadStore по ссылке result.
    """

    kind: Literal[MessageKind.TOOL_FINISHED] = MessageKind.TOOL_FINISHED
    turn_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    result: PayloadRef


class ToolFailed(Message):
    """Вызов call_id провалился; error — текст сбоя, тот же, что получила модель."""

    kind: Literal[MessageKind.TOOL_FAILED] = MessageKind.TOOL_FAILED
    turn_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    error: str


class ToolStopped(Message):
    """Вызов call_id не доработал, потому что ход остановлен или завершён; note —
    подпись для шага.
    """

    kind: Literal[MessageKind.TOOL_STOPPED] = MessageKind.TOOL_STOPPED
    turn_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    note: str = Field(min_length=1)


class TurnFinished(Message):
    """Ход turn_id закончился с исходом outcome; reason — текст для пользователя."""

    kind: Literal[MessageKind.TURN_FINISHED] = MessageKind.TURN_FINISHED
    turn_id: str = Field(min_length=1)
    outcome: TurnOutcome
    reason: str = ""


class RunStateChanged(Message):
    """Состояние запуска run_id записано в workflow_runs; получатель читает снимок
    оттуда.
    """

    kind: Literal[MessageKind.RUN_STATE_CHANGED] = MessageKind.RUN_STATE_CHANGED
    run_id: UUID
    status: str = Field(min_length=1)


class RunFinished(Message):
    """Запуск run_id завершён со статусом status; сообщений в этой области больше не
    будет.
    """

    kind: Literal[MessageKind.RUN_FINISHED] = MessageKind.RUN_FINISHED
    run_id: UUID
    status: str = Field(min_length=1)


class StreamAppended(Message):
    """Журнал канала channel вызова call_id дорос до size байт; сам текст лежит в файле
    журнала.
    """

    kind: Literal[MessageKind.STREAM_APPENDED] = MessageKind.STREAM_APPENDED
    call_id: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    size: int = Field(ge=0)
    closed: bool
    note: str = ""


class CanvasChanged(Message):
    """Показанный в панели файл path изменился; содержимое фронт запрашивает сам."""

    kind: Literal[MessageKind.CANVAS_CHANGED] = MessageKind.CANVAS_CHANGED
    path: str = Field(min_length=1)
    nonce: str
    revision: str
    size: int = Field(ge=0)
    closed: bool
    note: str = ""


class SignInRefreshRequested(Message):
    """Билет пользователя principal истекает: странице пора обновить вход."""

    kind: Literal[MessageKind.SIGNIN_REFRESH_REQUESTED] = (
        MessageKind.SIGNIN_REFRESH_REQUESTED
    )
    principal: str = Field(min_length=1)


class Notice(Message):
    """Уведомление пользователю: level задаёт способ показа, text — что случилось."""

    kind: Literal[MessageKind.NOTICE] = MessageKind.NOTICE
    level: NoticeLevel
    text: str = Field(min_length=1)


class LockLost(Message):
    """Держатель holder потерял блокировку области, взятую ради purpose."""

    kind: Literal[MessageKind.LOCK_LOST] = MessageKind.LOCK_LOST
    holder: str = Field(min_length=1)
    purpose: str = Field(min_length=1)


class StopRequested(Command):
    """Пользователь by_user попросил остановить область; просьба принята инстансом
    by_instance.
    """

    kind: Literal[CommandKind.STOP] = CommandKind.STOP
    by_user: int
    by_instance: str = Field(min_length=1)


AnyMessage = Annotated[
    TurnStarted
    | ModelAnswered
    | AnswerToken
    | AnswerClosed
    | AnswerInterrupted
    | ThinkingToken
    | ThinkingComplete
    | ThinkingClosed
    | StageStarted
    | StageQueries
    | StageEnded
    | ToolStarted
    | ToolFinished
    | ToolFailed
    | ToolStopped
    | TurnFinished
    | RunStateChanged
    | RunFinished
    | StreamAppended
    | CanvasChanged
    | SignInRefreshRequested
    | Notice
    | LockLost,
    Field(discriminator="kind"),
]

AnyCommand = Annotated[StopRequested, Field(discriminator="kind")]
