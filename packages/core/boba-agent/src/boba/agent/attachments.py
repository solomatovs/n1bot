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

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
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
from boba.agent.tags import AgentTag
from boba.agent.template import AgentTemplate, AgentTemplateFile

__all__ = [
    "Announced",
    "AttachmentKind",
    "ContextAttachments",
    "DateAttachment",
    "EnvironmentAttachment",
    "EnvironmentChange",
    "EnvironmentField",
    "EnvironmentSnapshot",
    "MaxTurnsAttachment",
    "ModelAttachment",
    "ModelIdentity",
    "PromptSnapshot",
    "QueuedCommandAttachment",
    "SkillBudget",
    "SkillListingAttachment",
    "SnapshotSkill",
    "SnapshotTool",
    "ToolsDeltaAttachment",
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


class EnvironmentSnapshot(PayloadModel):
    """Окружение процесса, которое объявляется модели; собирает вход процесса."""

    working_directory: str
    platform: str
    shell: str
    os_version: str

    def same_host(self, other: EnvironmentSnapshot) -> bool:
        """Платформа, оболочка и ОС не сменились: разницу можно описать полями."""
        if self.platform != other.platform:
            return False

        if self.shell != other.shell:
            return False

        return self.os_version == other.os_version


class EnvironmentField(StrEnum):
    """Поля окружения, у которых бывает разница."""

    WORKING_DIRECTORY = "workingDirectory"


class EnvironmentChange(PayloadModel):
    field: EnvironmentField
    previous: str = Field(
        validation_alias=AliasChoices("from", "previous"), serialization_alias="from"
    )


class EnvironmentAttachment(PayloadModel):
    """Окружение: полный блок при первом появлении, разница полей — потом."""

    type: str = AttachmentKind.ENVIRONMENT.value
    snapshot: EnvironmentSnapshot
    changes: Sequence[EnvironmentChange] | None = None


class ToolsDeltaAttachment(PayloadModel):
    """Изменение списка инструментов относительно объявленного."""

    type: str = AttachmentKind.TOOLS_DELTA.value
    added_names: Sequence[str] = ()
    removed_names: Sequence[str] = ()
    added_lines: Sequence[str] = ()


class SkillListingAttachment(PayloadModel):
    """Список скиллов: первый раз все, дальше только новые имена."""

    type: str = AttachmentKind.SKILL_LISTING.value
    content: str
    skill_count: int
    is_initial: bool
    names: Sequence[str]


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

    def environment(self) -> EnvironmentSnapshot | None:
        record = self._last(AttachmentKind.ENVIRONMENT)
        if record is None:
            return None

        attachment = EnvironmentAttachment.model_validate(
            record.attachment.model_dump()
        )

        return attachment.snapshot

    def tool_names(self) -> frozenset[str] | None:
        """Имена инструментов, известные модели: последний снимок плюс
        разницы после него; None — снимка в разговоре нет."""
        snapshot_at = self._last_index(AttachmentKind.PROMPT_SNAPSHOT)
        if snapshot_at is None:
            return None

        snapshot = self.snapshot()
        if snapshot is None:
            return None

        names: set[str] = set()
        for tool in snapshot.tools:
            names.add(tool.name)

        for record in self._conversation[snapshot_at + 1 :]:
            if not isinstance(record, AttachmentRecord):
                continue

            if record.attachment.type != AttachmentKind.TOOLS_DELTA.value:
                continue

            delta = ToolsDeltaAttachment.model_validate(record.attachment.model_dump())
            names.update(delta.added_names)
            names.difference_update(delta.removed_names)

        return frozenset(names)

    def skill_names(self) -> frozenset[str]:
        """Имена скиллов, уже перечисленных модели в этом разговоре."""
        names: set[str] = set()
        for record in self._conversation:
            if not isinstance(record, AttachmentRecord):
                continue

            if record.attachment.type != AttachmentKind.SKILL_LISTING.value:
                continue

            listing = SkillListingAttachment.model_validate(
                record.attachment.model_dump()
            )
            names.update(listing.names)

        return frozenset(names)

    def _is_kind(self, record: ChainRecord, kind: AttachmentKind) -> bool:
        if not isinstance(record, AttachmentRecord):
            return False

        return record.attachment.type == kind.value

    def _last_index(self, kind: AttachmentKind) -> int | None:
        for index in range(len(self._conversation) - 1, -1, -1):
            if self._is_kind(self._conversation[index], kind):
                return index

        return None

    def _last(self, kind: AttachmentKind) -> AttachmentRecord | None:
        for record in reversed(self._conversation):
            if not isinstance(record, AttachmentRecord):
                continue

            if record.attachment.type == kind.value:
                return record

        return None


class SkillBudget:
    """Бюджет списка скиллов: окно × знаков на токен × доля, не больше
    предела на скилл; строки сверх бюджета остаются одними именами."""

    FRACTION: ClassVar[float] = 0.01
    PER_SKILL: ClassVar[int] = 1536
    NAME_ONLY: ClassVar[str] = "- {name}"
    FULL: ClassVar[str] = "- {name}: {description}"

    def __init__(self, context_window: int, chars_per_token: int) -> None:
        self._total = int(context_window * chars_per_token * self.FRACTION)

    @property
    def total(self) -> int:
        return self._total

    def listing(self, skills: Sequence[SnapshotSkill]) -> str:
        used = 0
        lines: list[str] = []
        for skill in skills:
            full = self.FULL.format(
                name=skill.name, description=skill.description[: self.PER_SKILL]
            )
            if used + len(full) + len(lines) <= self._total:
                lines.append(full)
                used += len(full)
                continue

            short = self.NAME_ONLY.format(name=skill.name)
            lines.append(short)
            used += len(short)

        return "\n".join(lines)


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

    def environment(
        self,
        session: Session,
        current: EnvironmentSnapshot,
        announced: EnvironmentSnapshot | None,
    ) -> AttachmentRecord | None:
        """Окружение: полный блок, если его ещё не было или сменился хост;
        разница полей, если сменился рабочий каталог; None — без изменений."""
        if announced is None or not announced.same_host(current):
            text = self._templates.read(AgentTemplateFile.ENVIRONMENT).format(
                lines=self._environment_lines(current)
            )
            attachment = EnvironmentAttachment(snapshot=current)
            return self._system(session, attachment.payload(), text)

        if announced.working_directory == current.working_directory:
            return None

        change = EnvironmentChange(
            field=EnvironmentField.WORKING_DIRECTORY,
            previous=announced.working_directory,
        )
        line = (
            f" - Primary working directory: {current.working_directory} "
            f"(was {announced.working_directory})"
        )
        text = self._templates.read(AgentTemplateFile.ENVIRONMENT_UPDATE).format(
            lines=line
        )
        attachment = EnvironmentAttachment(snapshot=current, changes=[change])

        return self._system(session, attachment.payload(), text)

    def tools_delta(
        self,
        session: Session,
        added: Sequence[SnapshotTool],
        removed: Sequence[str],
    ) -> AttachmentRecord:
        """Разница списка инструментов: добавленные с описанием, снятые по имени."""
        sections: list[str] = []
        lines: list[str] = []
        for tool in added:
            lines.append(tool.text())

        if lines:
            sections.append(
                self._templates.read(AgentTemplateFile.TOOLS_ADDED).format(
                    lines="\n".join(lines)
                )
            )

        if removed:
            sections.append(
                self._templates.read(AgentTemplateFile.TOOLS_REMOVED).format(
                    names="\n".join(removed)
                )
            )
            sections.append(self._templates.read(AgentTemplateFile.TOOLS_AMBIENT))

        added_names: list[str] = []
        for tool in added:
            added_names.append(tool.name)

        attachment = ToolsDeltaAttachment(
            added_names=added_names, removed_names=list(removed), added_lines=lines
        )

        return self._system(session, attachment.payload(), "\n\n".join(sections))

    def skill_listing(
        self,
        session: Session,
        skills: Sequence[SnapshotSkill],
        budget: SkillBudget,
        initial: bool,
    ) -> AttachmentRecord:
        content = budget.listing(skills)
        text = self._templates.read(AgentTemplateFile.SKILL_LISTING).format(
            content=content
        )
        names: list[str] = []
        for skill in skills:
            names.append(skill.name)

        attachment = SkillListingAttachment(
            content=content, skill_count=len(skills), is_initial=initial, names=names
        )

        return self._system(session, attachment.payload(), text)

    def _environment_lines(self, snapshot: EnvironmentSnapshot) -> str:
        lines = [
            f" - Primary working directory: {snapshot.working_directory}",
            f" - Platform: {snapshot.platform}",
            f" - Shell: {snapshot.shell}",
            f" - OS Version: {snapshot.os_version}",
        ]

        return "\n".join(lines)

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
        wrapped = AgentTag.SYSTEM_REMINDER.wrap_block(text)

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
