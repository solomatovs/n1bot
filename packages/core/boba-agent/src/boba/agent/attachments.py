"""Вложения контекста: снимок промпта, модель, дата — и свёртка «что объявлено».

Вложение — запись, которой агент сообщает модели что-то помимо слов
пользователя (план, раздел 5.17); текст фиксируется один раз в `rendered`
по шаблонам пакета. Снимок `prompt_snapshot` модели не отправляется: по нему
сборщик берёт системный промпт, инструменты и режим подачи вложений. Свёртка
Announced считает по разговору, что модели уже объявлено.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from boba.agent.ids import Clock, IdMint
from boba.agent.profile import ModelProfile
from boba.agent.queue import QueueItem, QueueKind
from boba.agent.records import (
    AttachmentPayload,
    AttachmentRecord,
    AttachmentRole,
    ChainRecord,
    OriginKind,
    RenderedMessage,
)
from boba.agent.session import Session
from boba.agent.template import AgentTemplate, AgentTemplateFile

__all__ = [
    "Announced",
    "AttachmentKind",
    "ContextAttachments",
    "DateAttachment",
    "MaxTurnsAttachment",
    "ModelAttachment",
    "ModelIdentity",
    "PromptSnapshot",
    "QueuedCommandAttachment",
    "SnapshotSkill",
    "SnapshotTool",
]


class AttachmentKind(StrEnum):
    """Виды вложений первой версии."""

    PROMPT_SNAPSHOT = "prompt_snapshot"
    MODEL = "model"
    DATE = "date"
    ENVIRONMENT = "environment"
    TOOLS_DELTA = "tools_delta"
    SKILL_LISTING = "skill_listing"
    QUEUED_COMMAND = "queued_command"
    MAX_TURNS_REACHED = "max_turns_reached"


class PayloadModel(BaseModel):
    """Основа данных вложения: имена полей на проводе — camelCase."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="ignore"
    )

    def payload(self) -> AttachmentPayload:
        """Данные вложения в виде, в каком они лягут в журнал: как после JSON."""
        document = self.model_dump(by_alias=True, mode="json")

        return AttachmentPayload.model_validate(document)


class SnapshotTool(PayloadModel):
    name: str
    description: str
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")

    def text(self) -> str:
        """Текст описания для оценки места в окне."""
        return f"{self.name}: {self.description}"


class SnapshotSkill(PayloadModel):
    name: str
    description: str


class PromptSnapshot(PayloadModel):
    """Снимок: системный промпт секциями, инструменты, скиллы, модель, режим."""

    type: str = AttachmentKind.PROMPT_SNAPSHOT.value
    system_prompt: Sequence[str]
    tools: Sequence[SnapshotTool] = ()
    skills: Sequence[SnapshotSkill] = ()
    model: str
    system_turns: bool


class ModelIdentity(PayloadModel):
    model_id: str


class ModelAttachment(PayloadModel):
    type: str = AttachmentKind.MODEL.value
    identity: ModelIdentity
    text: str


class DateAttachment(PayloadModel):
    type: str = AttachmentKind.DATE.value
    date: str
    changed: bool


class QueuedCommandAttachment(PayloadModel):
    """Сообщение, подмешанное посреди хода из очереди (раздел 5.7, путь 2)."""

    type: str = AttachmentKind.QUEUED_COMMAND.value
    prompt: str
    source_uuid: str
    origin: OriginKind
    is_meta: bool


class MaxTurnsAttachment(PayloadModel):
    """Предел шагов хода достигнут; модели не отправляется."""

    type: str = AttachmentKind.MAX_TURNS_REACHED.value
    max_turns: int
    turn_count: int


class Announced:
    """Что модели уже объявлено: последние снимок, модель и дата разговора."""

    def __init__(self, conversation: Sequence[ChainRecord]) -> None:
        self._conversation = conversation

    def snapshot(self) -> PromptSnapshot | None:
        record = self._last(AttachmentKind.PROMPT_SNAPSHOT)
        if record is None:
            return None

        return PromptSnapshot.model_validate(record.attachment.model_dump())

    def model_id(self) -> str | None:
        record = self._last(AttachmentKind.MODEL)
        if record is None:
            return None

        model = ModelAttachment.model_validate(record.attachment.model_dump())

        return model.identity.model_id

    def date(self) -> str | None:
        record = self._last(AttachmentKind.DATE)
        if record is None:
            return None

        return DateAttachment.model_validate(record.attachment.model_dump()).date

    def _last(self, kind: AttachmentKind) -> AttachmentRecord | None:
        for record in reversed(self._conversation):
            if not isinstance(record, AttachmentRecord):
                continue

            if record.attachment.type == kind.value:
                return record

        return None


class ContextAttachments:
    """Создаёт записи вложений контекста и кладёт их в сессию."""

    DATE_LENGTH: ClassVar[int] = 10

    def __init__(
        self, templates: AgentTemplate, ids: IdMint, clock: Clock, version: str
    ) -> None:
        self._templates = templates
        self._ids = ids
        self._clock = clock
        self._version = version

    def today(self) -> str:
        return self._clock.now()[: self.DATE_LENGTH]

    def snapshot(self, session: Session, snapshot: PromptSnapshot) -> AttachmentRecord:
        """Снимок без `rendered`: модели он не отправляется."""
        return session.add(
            AttachmentRecord(
                uuid=self._ids.uuid(),
                parent_uuid=session.last_chain_uuid(),
                timestamp=self._clock.now(),
                session_id=session.id.value,
                version=self._version,
                attachment=snapshot.payload(),
            )
        )

    def model(self, session: Session, profile: ModelProfile) -> AttachmentRecord:
        text = self._templates.read(AgentTemplateFile.MODEL_PLAIN).format(
            model_id=profile.model_id
        )
        attachment = ModelAttachment(
            identity=ModelIdentity(model_id=profile.model_id), text=text
        )

        return self._system(session, attachment.payload(), text)

    def date(self, session: Session, today: str, changed: bool) -> AttachmentRecord:
        template = AgentTemplateFile.DATE
        if changed:
            template = AgentTemplateFile.DATE_CHANGED

        text = self._templates.read(template).format(date=today)
        attachment = DateAttachment(date=today, changed=changed)

        return self._system(session, attachment.payload(), text)

    def queued(
        self, session: Session, items: Sequence[QueueItem], agent_name: str
    ) -> Sequence[AttachmentRecord]:
        """Подмешанные элементы очереди — по вложению на элемент, роль `user`.
        Уведомление рядом с сообщением человека получает обёртку, которая
        называет это сообщение настоящим вводом."""
        with_user = False
        for item in items:
            if item.kind is QueueKind.PROMPT:
                with_user = True

        records: list[AttachmentRecord] = []
        for item in items:
            records.append(self._queued_one(session, item, agent_name, with_user))

        return records

    def max_turns(
        self, session: Session, max_turns: int, turn_count: int
    ) -> AttachmentRecord:
        """Вложение без `rendered`: модели оно не уходит."""
        attachment = MaxTurnsAttachment(max_turns=max_turns, turn_count=turn_count)

        return session.add(
            AttachmentRecord(
                uuid=self._ids.uuid(),
                parent_uuid=session.last_chain_uuid(),
                timestamp=self._clock.now(),
                session_id=session.id.value,
                version=self._version,
                attachment=attachment.payload(),
            )
        )

    def _queued_one(
        self, session: Session, item: QueueItem, agent_name: str, with_user: bool
    ) -> AttachmentRecord:
        origin = OriginKind.HUMAN
        template = AgentTemplateFile.QUEUED_HUMAN
        if item.kind is QueueKind.TASK_NOTIFICATION:
            origin = OriginKind.TASK_NOTIFICATION
            template = AgentTemplateFile.QUEUED_NOTIFICATION
            if with_user:
                template = AgentTemplateFile.QUEUED_NOTIFICATION_WITH_USER

        text = self._templates.read(template).format(text=item.text, agent=agent_name)
        attachment = QueuedCommandAttachment(
            prompt=item.text,
            source_uuid=item.id,
            origin=origin,
            is_meta=item.is_meta,
        )

        return session.add(
            AttachmentRecord(
                uuid=self._ids.uuid(),
                parent_uuid=session.last_chain_uuid(),
                timestamp=self._clock.now(),
                session_id=session.id.value,
                version=self._version,
                attachment=attachment.payload(),
                rendered=[RenderedMessage(content=text)],
                rendered_role=AttachmentRole.USER,
            )
        )

    def _system(
        self, session: Session, payload: AttachmentPayload, text: str
    ) -> AttachmentRecord:
        wrapped = self._templates.read(AgentTemplateFile.SYSTEM_REMINDER).format(
            content=text
        )

        return session.add(
            AttachmentRecord(
                uuid=self._ids.uuid(),
                parent_uuid=session.last_chain_uuid(),
                timestamp=self._clock.now(),
                session_id=session.id.value,
                version=self._version,
                attachment=payload,
                rendered=[RenderedMessage(content=wrapped)],
                rendered_role=AttachmentRole.SYSTEM,
            )
        )
