"""Сообщения и команды шины: неизменяемые модели с дискриминатором kind, в которых
события хода, запуска, журнала и канваса покидают исполнителя.

Ошибки: своих не выпускает; негодные данные отвергает pydantic.ValidationError.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.messaging.payloads import PayloadRef

__all__ = [
    "AnswerInterrupted",
    "AnyCommand",
    "AnyMessage",
    "CanvasChanged",
    "CatalogChanged",
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
    "Notice",
    "NoticeLevel",
    "RunFinished",
    "RunListChanged",
    "RunStateChanged",
    "SignInRefreshRequested",
    "StopRequested",
    "StreamAppended",
    "StudioProfileChanged",
    "ThreadChanged",
    "ThreadRewound",
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
    ANSWER_INTERRUPTED = "answer_interrupted"
    TURN_FINISHED = "turn_finished"
    AGENT_UPDATE = "agent_update"
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
    CATALOG_CHANGED = "catalog_changed"


_HOLDER_ONLY: frozenset[MessageKind] = frozenset(
    {
        MessageKind.TURN_STARTED,
        MessageKind.ANSWER_INTERRUPTED,
        MessageKind.TURN_FINISHED,
        MessageKind.AGENT_UPDATE,
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


class AnswerInterrupted(Message):
    """Ответ шага key оборван остановкой; note — пометка, которую получатель дописывает
    к тексту.
    """

    kind: Literal[MessageKind.ANSWER_INTERRUPTED] = MessageKind.ANSWER_INTERRUPTED
    turn_id: str = Field(min_length=1)
    key: str = Field(min_length=1)
    note: str = Field(min_length=1)


class TurnFinished(Message):
    """Ход turn_id закончился с исходом outcome; reason — текст для пользователя."""

    HOLDER_GONE: ClassVar[str] = "stopped: the process running this turn is gone"
    """Причина, с которой сторож закрывает ход умершего держателя."""

    kind: Literal[MessageKind.TURN_FINISHED] = MessageKind.TURN_FINISHED
    turn_id: str = Field(min_length=1)
    outcome: TurnOutcome
    reason: str = ""


class AgentUpdate(Message):
    """Уведомление `session/update` агента, которое держатель хода turn_id
    пересылает вкладкам и инстансам треда. Тело — JSON уведомления: мелкое
    (куски текста) едет в самом сообщении (inline), крупное (вызов с его
    аргументами и итогом) — по ссылке (stored); заполнено ровно одно из них.
    """

    kind: Literal[MessageKind.AGENT_UPDATE] = MessageKind.AGENT_UPDATE
    turn_id: str = Field(min_length=1)
    inline: dict[str, object] | None = None
    stored: PayloadRef | None = None

    @model_validator(mode="after")
    def _exactly_one_body(self) -> Self:
        if (self.inline is None) == (self.stored is None):
            msg = (
                "agent_update: exactly one of inline or stored expected, got "
                f"inline={self.inline is not None}, stored={self.stored is not None}"
            )
            raise ValueError(msg)

        return self


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
    """Черновик workflow записан (revision) или сброшен к сохранённому; by_sid —
    сокет вкладки, которая его изменила, чтобы она не применяла своё же изменение.
    """

    kind: Literal[MessageKind.WORKFLOW_DRAFT_CHANGED] = (
        MessageKind.WORKFLOW_DRAFT_CHANGED
    )
    workflow_id: UUID
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


class CatalogChanged(Message):
    """Каталог данных изменился: процесс process_id (создан, переименован,
    удалён; с version — опубликована версия), черновик draft_id (порция или
    закрытие), подключение connection_id (новая версия снимка, версии забыты),
    синхронизация sync_id либо upgrade upgrade_id (старт, ход, итог).
    Заполнен ровно один из пяти идентификаторов; version идёт только с
    process_id.
    """

    kind: Literal[MessageKind.CATALOG_CHANGED] = MessageKind.CATALOG_CHANGED
    process_id: UUID | None = None
    version: int | None = None
    draft_id: UUID | None = None
    connection_id: UUID | None = None
    sync_id: UUID | None = None
    upgrade_id: UUID | None = None
    action: ChangeAction

    @model_validator(mode="after")
    def _exactly_one_target(self) -> Self:
        targets = (
            self.process_id,
            self.draft_id,
            self.connection_id,
            self.sync_id,
            self.upgrade_id,
        )

        filled = 0
        for target in targets:
            if target is None:
                continue

            filled += 1

        if filled != 1:
            msg = (
                "catalog_changed: exactly one of process_id, draft_id,"
                f" connection_id, sync_id, upgrade_id expected, got {filled} of "
                f"them set: process_id={self.process_id}, draft_id={self.draft_id}, "
                f"connection_id={self.connection_id}, sync_id={self.sync_id}, "
                f"upgrade_id={self.upgrade_id}"
            )
            raise ValueError(msg)

        if self.version is not None and self.process_id is None:
            msg = (
                "catalog_changed: version names a published version and goes "
                f"only with process_id, got version={self.version} without it"
            )
            raise ValueError(msg)

        return self


class StopRequested(Command):
    """Пользователь by_user попросил остановить область; просьба принята инстансом
    by_instance.
    """

    kind: Literal[CommandKind.STOP] = CommandKind.STOP
    by_user: UUID
    by_instance: str = Field(min_length=1)


AnyMessage = Annotated[
    TurnStarted
    | AnswerInterrupted
    | TurnFinished
    | AgentUpdate
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
    | ConnectionsChanged
    | CatalogChanged,
    Field(discriminator="kind"),
]

AnyCommand = Annotated[StopRequested, Field(discriminator="kind")]
