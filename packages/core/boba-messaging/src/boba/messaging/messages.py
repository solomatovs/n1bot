"""Сообщения и команды шины: неизменяемые модели с дискриминатором kind, в которых
события хода, запуска, журнала и канваса покидают исполнителя.

Ошибки: своих не выпускает; негодные данные отвергает pydantic.ValidationError.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar, Literal
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
    "ChangeAction",
    "ChatSettingsChanged",
    "Command",
    "CommandKind",
    "ConnectionsChanged",
    "ElementRemoved",
    "ElementShown",
    "FeedbackChanged",
    "LockLost",
    "Message",
    "MessageKind",
    "ModelAnswered",
    "Notice",
    "NoticeLevel",
    "RunFinished",
    "RunListChanged",
    "RunStateChanged",
    "SignInRefreshRequested",
    "StageEnded",
    "StageQueries",
    "StageStarted",
    "StopRequested",
    "StreamAppended",
    "StudioProfileChanged",
    "ThinkingClosed",
    "TokensSpent",
    "ThinkingComplete",
    "ThinkingToken",
    "ThreadChanged",
    "ThreadRewound",
    "ToolFailed",
    "ToolFinished",
    "ToolStarted",
    "ToolStopped",
    "TurnFinished",
    "TurnOutcome",
    "TurnStarted",
    "WorkflowChanged",
    "WorkflowDraftChanged",
]


class MessageKind(StrEnum):
    """Виды сообщений шины; значение хранится в live_events.kind и в теле конверта."""

    @property
    def requires_lock(self) -> bool:
        """Публиковать вправе только держатель области: события хода и запуска."""
        return self in _HOLDER_ONLY

    TURN_STARTED = "turn_started"
    MODEL_ANSWERED = "model_answered"
    ANSWER_TOKEN = "answer_token"  # noqa: S105
    ANSWER_CLOSED = "answer_closed"
    ANSWER_INTERRUPTED = "answer_interrupted"
    THINKING_TOKEN = "thinking_token"  # noqa: S105
    THINKING_COMPLETE = "thinking_complete"
    THINKING_CLOSED = "thinking_closed"
    TOKENS_SPENT = "tokens_spent"  # noqa: S105
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
    RUN_LIST_CHANGED = "run_list_changed"
    WORKFLOW_CHANGED = "workflow_changed"
    CONNECTIONS_CHANGED = "connections_changed"
    THREAD_REWOUND = "thread_rewound"
    ELEMENT_SHOWN = "element_shown"
    THREAD_CHANGED = "thread_changed"
    WORKFLOW_DRAFT_CHANGED = "workflow_draft_changed"
    FEEDBACK_CHANGED = "feedback_changed"
    ELEMENT_REMOVED = "element_removed"
    CHAT_SETTINGS_CHANGED = "chat_settings_changed"
    STUDIO_PROFILE_CHANGED = "studio_profile_changed"


_HOLDER_ONLY: frozenset[MessageKind] = frozenset(
    {
        MessageKind.TURN_STARTED,
        MessageKind.MODEL_ANSWERED,
        MessageKind.ANSWER_TOKEN,
        MessageKind.ANSWER_CLOSED,
        MessageKind.ANSWER_INTERRUPTED,
        MessageKind.THINKING_TOKEN,
        MessageKind.THINKING_COMPLETE,
        MessageKind.THINKING_CLOSED,
        MessageKind.TOKENS_SPENT,
        MessageKind.STAGE_STARTED,
        MessageKind.STAGE_QUERIES,
        MessageKind.STAGE_ENDED,
        MessageKind.TOOL_STARTED,
        MessageKind.TOOL_FINISHED,
        MessageKind.TOOL_FAILED,
        MessageKind.TOOL_STOPPED,
        MessageKind.TURN_FINISHED,
        MessageKind.RUN_STATE_CHANGED,
        MessageKind.RUN_FINISHED,
        MessageKind.STREAM_APPENDED,
        MessageKind.ELEMENT_SHOWN,
    }
)


class CommandKind(StrEnum):
    """Виды команд шины; значение хранится в live_commands.action."""

    STOP = "stop"


class TurnOutcome(StrEnum):
    """Чем закончился ход чата: успехом, остановкой или сбоем."""

    OK = "ok"
    STOPPED = "stopped"
    FAILED = "failed"


class ChangeAction(StrEnum):
    """Что случилось с записью списка пользователя."""

    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"


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
    ответ, question — текст вопроса по ссылке, чтобы его нарисовали все вкладки треда.
    """

    kind: Literal[MessageKind.TURN_STARTED] = MessageKind.TURN_STARTED
    turn_id: str = Field(min_length=1)
    key: str = Field(min_length=1)
    question: PayloadRef


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


class TokensSpent(Message):
    """Прогон модели закончился: сколько токенов он стоил. key адресует шаг
    рассуждений этого прогона; шага может не быть, тогда расход виден только в
    итоге хода.
    """

    kind: Literal[MessageKind.TOKENS_SPENT] = MessageKind.TOKENS_SPENT
    turn_id: str = Field(min_length=1)
    key: str = ""
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(
        default=0,
        ge=0,
        description="Часть output_tokens, ушедшая в рассуждения; 0 — не сообщено.",
    )


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

    HOLDER_GONE: ClassVar[str] = "stopped: the process running this turn is gone"
    """Причина, с которой сторож закрывает ход умершего держателя."""

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


class RunListChanged(Message):
    """Список запусков пользователя изменился: запуск run_id workflow workflow_name
    появился или сменил статус на status.
    """

    kind: Literal[MessageKind.RUN_LIST_CHANGED] = MessageKind.RUN_LIST_CHANGED
    run_id: UUID
    workflow_id: UUID | None
    workflow_name: str
    status: str = Field(min_length=1)


class WorkflowChanged(Message):
    """Workflow workflow_id пользователя сохранён или удалён."""

    kind: Literal[MessageKind.WORKFLOW_CHANGED] = MessageKind.WORKFLOW_CHANGED
    workflow_id: UUID
    name: str = Field(min_length=1)
    action: ChangeAction


class ConnectionsChanged(Message):
    """Соединение connection_id пользователя создано, изменено или удалено."""

    kind: Literal[MessageKind.CONNECTIONS_CHANGED] = MessageKind.CONNECTIONS_CHANGED
    connection_id: UUID
    name: str = Field(min_length=1)
    action: ChangeAction


class ThreadRewound(Message):
    """История треда обрезана до вопроса turn_id с новым текстом: ленту надо
    перечитать из истории.
    """

    kind: Literal[MessageKind.THREAD_REWOUND] = MessageKind.THREAD_REWOUND
    turn_id: str = Field(min_length=1)


class ElementShown(Message):
    """Вызов call_id хода turn_id показал элемент ленты (файл, карточку); тело
    элемента лежит по ссылке element.
    """

    kind: Literal[MessageKind.ELEMENT_SHOWN] = MessageKind.ELEMENT_SHOWN
    turn_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    element: PayloadRef


class ThreadChanged(Message):
    """Тред thread_id пользователя создан, переименован или удалён."""

    kind: Literal[MessageKind.THREAD_CHANGED] = MessageKind.THREAD_CHANGED
    thread_id: str = Field(min_length=1)
    name: str
    action: ChangeAction


class WorkflowDraftChanged(Message):
    """Черновик билдера key пользователя записан (revision) или удалён; by_sid — сокет
    вкладки, которая его изменила, чтобы она не применяла своё же изменение.
    """

    kind: Literal[MessageKind.WORKFLOW_DRAFT_CHANGED] = (
        MessageKind.WORKFLOW_DRAFT_CHANGED
    )
    key: str = Field(min_length=1)
    revision: int = Field(ge=0)
    by_sid: str
    action: ChangeAction


class FeedbackChanged(Message):
    """Оценка шага step_id поставлена (value 0/1 с комментарием) или снята (value
    None).
    """

    kind: Literal[MessageKind.FEEDBACK_CHANGED] = MessageKind.FEEDBACK_CHANGED
    step_id: str = Field(min_length=1)
    value: int | None
    comment: str = ""


class ElementRemoved(Message):
    """Элемент ленты element_id удалён пользователем."""

    kind: Literal[MessageKind.ELEMENT_REMOVED] = MessageKind.ELEMENT_REMOVED
    element_id: str = Field(min_length=1)


class ChatSettingsChanged(Message):
    """Пользователь сохранил настройки чата для профиля profile из сессии
    by_session; остальные его вкладки на этом профиле пересобирают агента.
    """

    kind: Literal[MessageKind.CHAT_SETTINGS_CHANGED] = MessageKind.CHAT_SETTINGS_CHANGED
    profile: str = Field(min_length=1)
    by_session: str


class StudioProfileChanged(Message):
    """Пользователь выбрал профиль studio из вкладки с сокетом by_sid."""

    kind: Literal[MessageKind.STUDIO_PROFILE_CHANGED] = (
        MessageKind.STUDIO_PROFILE_CHANGED
    )
    profile: str = Field(min_length=1)
    by_sid: str


class StopRequested(Command):
    """Пользователь by_user попросил остановить область; просьба принята инстансом
    by_instance.
    """

    kind: Literal[CommandKind.STOP] = CommandKind.STOP
    by_user: UUID
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
    | TokensSpent
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
    | LockLost
    | RunListChanged
    | ThreadRewound
    | ElementShown
    | ThreadChanged
    | WorkflowDraftChanged
    | FeedbackChanged
    | ElementRemoved
    | ChatSettingsChanged
    | StudioProfileChanged
    | WorkflowChanged
    | ConnectionsChanged,
    Field(discriminator="kind"),
]

AnyCommand = Annotated[StopRequested, Field(discriminator="kind")]
