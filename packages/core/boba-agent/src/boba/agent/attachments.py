"""Вложения контекста: снимок промпта, модель, дата — и свёртка «что объявлено».

Вложение — запись, которой агент сообщает модели что-то помимо слов
пользователя (план, раздел 5.17); текст фиксируется один раз в `rendered`
по шаблонам пакета. Снимок `prompt_snapshot` модели не отправляется: по нему
сборщик берёт системный промпт, инструменты и режим подачи вложений. Свёртка
Announced считает по разговору, что модели уже объявлено. Инструкции
серверов MCP идут вложением `mcp_instructions_delta`, а не системным
промптом: смена инструкций посреди сессии не переписывает начало запроса
(docs/prompt_assembly/texts/attachments/mcp_instructions_delta.md).

ContextAttachments строит записи, но в сессию их не кладёт: родителя
называет вызывающий. Так сжатие собирает всю пачку — границу, дату,
пересказ, вложения — до того, как первая из них попадёт в сессию.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
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
from boba.agent.tags import AgentTag
from boba.agent.template import AgentTemplate, AgentTemplateFile
from boba.toolkit.mcp import McpToolNames

__all__ = [
    "Announced",
    "AttachmentKind",
    "ConnectedServer",
    "ContextAttachments",
    "DateAttachment",
    "EnvironmentAttachment",
    "EnvironmentChange",
    "EnvironmentField",
    "EnvironmentSnapshot",
    "MaxTurnsAttachment",
    "McpInstructionsAttachment",
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
    MCP_INSTRUCTIONS_DELTA = "mcp_instructions_delta"
    SKILL_LISTING = "skill_listing"
    QUEUED_COMMAND = "queued_command"
    MAX_TURNS_REACHED = "max_turns_reached"


@dataclass(frozen=True)
class ConnectedServer:
    """Подключённый сервер MCP сессии и его инструкции из server/discover;
    пустая строка — сервер инструкций не дал."""

    name: str
    instructions: str


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
    """Изменение списка инструментов относительно объявленного; readded —
    добавленные, которые этот разговор уже объявлял и потом снимал: их
    строк в added_lines нет."""

    type: str = AttachmentKind.TOOLS_DELTA.value
    added_names: Sequence[str] = ()
    removed_names: Sequence[str] = ()
    added_lines: Sequence[str] = ()
    readded_names: Sequence[str] = ()


class McpInstructionsAttachment(PayloadModel):
    """Изменение инструкций серверов MCP относительно объявленного: блоки
    `## <сервер>` появившихся серверов и серверов с новым текстом, имена
    отключившихся (docs/prompt_assembly, mcp_instructions_delta)."""

    type: str = AttachmentKind.MCP_INSTRUCTIONS_DELTA.value
    added_names: Sequence[str] = ()
    added_blocks: Sequence[str] = ()
    added_server_instructions: Sequence[str] = ()
    removed_names: Sequence[str] = ()


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

    def withdrawn_tools(self) -> frozenset[str]:
        """Имена, которые разговор объявлял и потом снял разницей, а заново
        с тех пор не объявлял."""
        withdrawn: set[str] = set()
        for delta in self._tool_deltas():
            withdrawn.difference_update(delta.added_names)
            withdrawn.update(delta.removed_names)

        return frozenset(withdrawn)

    def instructions(self) -> Mapping[str, str]:
        """Объявленные инструкции серверов MCP по имени сервера: свёртка
        вложений mcp_instructions_delta разговора."""
        announced: dict[str, str] = {}
        for record in self._conversation:
            if not isinstance(record, AttachmentRecord):
                continue

            if record.attachment.type != AttachmentKind.MCP_INSTRUCTIONS_DELTA.value:
                continue

            delta = McpInstructionsAttachment.model_validate(
                record.attachment.model_dump()
            )
            texts = zip(delta.added_names, delta.added_server_instructions, strict=True)
            for name, text in texts:
                announced[name] = text

            for name in delta.removed_names:
                announced.pop(name, None)

        return announced

    def _tool_deltas(self) -> Iterator[ToolsDeltaAttachment]:
        for record in self._conversation:
            if not isinstance(record, AttachmentRecord):
                continue

            if record.attachment.type != AttachmentKind.TOOLS_DELTA.value:
                continue

            yield ToolsDeltaAttachment.model_validate(record.attachment.model_dump())

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
    """Строит записи вложений контекста одной сессии; добавляет их вызывающий."""

    DATE_LENGTH: ClassVar[int] = 10
    LISTED_UP_TO: ClassVar[int] = 30
    """Сколько снятых инструментов MCP перечисляется по строке; больше —
    группами `mcp__<сервер>__*` с числом."""

    def __init__(
        self,
        templates: AgentTemplate,
        ids: IdMint,
        clock: Clock,
        version: str,
        session_id: str,
    ) -> None:
        self._templates = templates
        self._ids = ids
        self._clock = clock
        self._version = version
        self._session_id = session_id
        self._names = McpToolNames()

    def today(self) -> str:
        return self._clock.now()[: self.DATE_LENGTH]

    def snapshot(
        self, parent: str | None, snapshot: PromptSnapshot
    ) -> AttachmentRecord:
        """Снимок без `rendered`: модели он не отправляется."""
        return self._silent(parent, snapshot.payload())

    def model(self, parent: str | None, profile: ModelProfile) -> AttachmentRecord:
        text = self._templates.read(AgentTemplateFile.MODEL_PLAIN).format(
            model_id=profile.model_id
        )
        attachment = ModelAttachment(
            identity=ModelIdentity(model_id=profile.model_id), text=text
        )

        return self._system(parent, attachment.payload(), text)

    def date(self, parent: str | None, today: str, changed: bool) -> AttachmentRecord:
        template = AgentTemplateFile.DATE
        if changed:
            template = AgentTemplateFile.DATE_CHANGED

        text = self._templates.read(template).format(date=today)
        attachment = DateAttachment(date=today, changed=changed)

        return self._system(parent, attachment.payload(), text)

    def environment(
        self,
        parent: str | None,
        current: EnvironmentSnapshot,
        announced: EnvironmentSnapshot | None,
    ) -> AttachmentRecord | None:
        """Окружение: полный блок, если его ещё не было или сменился хост;
        разница полей, если сменился рабочий каталог; None — без изменений."""
        if announced is None:
            return self.environment_block(parent, current)

        if not announced.same_host(current):
            return self.environment_block(parent, current)

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

        return self._system(parent, attachment.payload(), text)

    def environment_block(
        self, parent: str | None, current: EnvironmentSnapshot
    ) -> AttachmentRecord:
        """Окружение полным блоком: первое объявление и после сжатия."""
        text = self._templates.read(AgentTemplateFile.ENVIRONMENT).format(
            lines=self._environment_lines(current)
        )
        attachment = EnvironmentAttachment(snapshot=current)

        return self._system(parent, attachment.payload(), text)

    def tools_delta(
        self,
        parent: str | None,
        added: Sequence[SnapshotTool],
        removed: Sequence[str],
        readded: frozenset[str],
    ) -> AttachmentRecord:
        """Разница списка инструментов (docs/prompt_assembly, deferred_tools_delta
        без поиска инструментов): новые — с описанием; вернувшиеся (readded) —
        числом и именами, инструменты MCP — как вернувшиеся с сервером;
        снятые — по имени, инструменты MCP — как ушедшие с сервером."""
        lines: list[str] = []
        for tool in added:
            if tool.name in readded:
                continue

            lines.append(tool.text())

        sections: list[str] = []
        if lines:
            sections.append(
                self._templates.read(AgentTemplateFile.TOOLS_ADDED).format(
                    lines="\n".join(lines)
                )
            )

        returned = sorted(readded)
        sections.extend(self._readded(returned))
        sections.extend(self._removed(removed))

        added_names: list[str] = []
        for tool in added:
            added_names.append(tool.name)

        attachment = ToolsDeltaAttachment(
            added_names=added_names,
            removed_names=list(removed),
            added_lines=lines,
            readded_names=returned,
        )

        return self._system(parent, attachment.payload(), "\n\n".join(sections))

    def instructions(
        self,
        parent: str | None,
        connected: Sequence[ConnectedServer],
        announced: Mapping[str, str],
    ) -> AttachmentRecord | None:
        """Инструкции серверов MCP относительно объявленных (docs/prompt_assembly,
        mcp_instructions_delta): сервер с инструкциями, которых модель не
        знает или знает другими, — блоком `## <сервер>`; объявленный сервер,
        который больше не подключён, — именем; None — сообщать нечего."""
        added = list(self._instructed(connected, announced))
        removed = list(self._departed(connected, announced))
        if not added and not removed:
            return None

        names: list[str] = []
        blocks: list[str] = []
        texts: list[str] = []
        for server in added:
            names.append(server.name)
            blocks.append(f"## {server.name}\n{server.instructions}")
            texts.append(server.instructions)

        sections: list[str] = []
        if blocks:
            sections.append(
                self._templates.read(AgentTemplateFile.MCP_INSTRUCTIONS).format(
                    blocks="\n\n".join(blocks)
                )
            )

        if removed:
            sections.append(
                self._templates.read(AgentTemplateFile.MCP_INSTRUCTIONS_REMOVED).format(
                    names="\n".join(removed)
                )
            )
            sections.append(self._templates.read(AgentTemplateFile.AMBIENT))

        attachment = McpInstructionsAttachment(
            added_names=names,
            added_blocks=blocks,
            added_server_instructions=texts,
            removed_names=removed,
        )

        return self._system(parent, attachment.payload(), "\n\n".join(sections))

    def _instructed(
        self, connected: Sequence[ConnectedServer], announced: Mapping[str, str]
    ) -> Iterator[ConnectedServer]:
        """Серверы по имени, чьих инструкций модель не знает или знает
        другими; сервер без инструкций — не в счёт."""
        for server in sorted(connected, key=self._server_name):
            if not server.instructions:
                continue

            if announced.get(server.name) == server.instructions:
                continue

            yield server

    @staticmethod
    def _departed(
        connected: Sequence[ConnectedServer], announced: Mapping[str, str]
    ) -> Iterator[str]:
        """Объявленные серверы, которых нет среди подключённых, по имени."""
        present: set[str] = set()
        for server in connected:
            present.add(server.name)

        for name in sorted(announced):
            if name not in present:
                yield name

    @staticmethod
    def _server_name(server: ConnectedServer) -> str:
        return server.name

    def _readded(self, names: Sequence[str]) -> Iterator[str]:
        """Разделы о вернувшихся инструментах: сначала инструменты MCP, затем
        прочие."""
        servers = list(self._picked(names, True))
        own = list(self._picked(names, False))
        if servers:
            yield self._templates.read(AgentTemplateFile.TOOLS_READDED_MCP).format(
                subject=self._counted(len(servers)), names=self._grouped(servers)
            )

        if own:
            yield self._templates.read(AgentTemplateFile.TOOLS_READDED).format(
                subject=self._counted(len(own)), names=", ".join(own)
            )

    def _removed(self, names: Sequence[str]) -> Iterator[str]:
        """Разделы о снятых инструментах и замыкающая строка о фоновом
        контексте; без снятых — ничего."""
        servers = list(self._picked(names, True))
        own = list(self._picked(names, False))
        if len(servers) > self.LISTED_UP_TO:
            yield self._templates.read(AgentTemplateFile.TOOLS_REMOVED_MCP_MANY).format(
                count=len(servers), names=self._grouped(servers)
            )
        elif servers:
            yield self._templates.read(AgentTemplateFile.TOOLS_REMOVED_MCP).format(
                names="\n".join(servers)
            )

        if own:
            yield self._templates.read(AgentTemplateFile.TOOLS_REMOVED).format(
                names="\n".join(own)
            )

        if names:
            yield self._templates.read(AgentTemplateFile.AMBIENT)

    def _picked(self, names: Sequence[str], of_servers: bool) -> Iterator[str]:
        """Имена инструментов серверов MCP (of_servers) либо прочие, в
        исходном порядке."""
        for name in names:
            served = self._names.parse(name) is not None
            if served is of_servers:
                yield name

    def _grouped(self, names: Sequence[str]) -> str:
        """Имена инструментов MCP группами серверов `mcp__<сервер>__*` по
        алфавиту; у группы больше одного имени — число в скобках."""
        groups: Counter[str] = Counter()
        for name in names:
            groups[self._names.group(name)] += 1

        shown: list[str] = []
        for group in sorted(groups):
            count = groups[group]
            if count > 1:
                shown.append(f"{group} ({count})")
                continue

            shown.append(group)

        return ", ".join(shown)

    @staticmethod
    def _counted(count: int) -> str:
        if count == 1:
            return "1 tool is"

        return f"{count} tools are"

    def skill_listing(
        self,
        parent: str | None,
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

        return self._system(parent, attachment.payload(), text)

    def _environment_lines(self, snapshot: EnvironmentSnapshot) -> str:
        lines = [
            f" - Primary working directory: {snapshot.working_directory}",
            f" - Platform: {snapshot.platform}",
            f" - Shell: {snapshot.shell}",
            f" - OS Version: {snapshot.os_version}",
        ]

        return "\n".join(lines)

    def queued(
        self, parent: str | None, items: Sequence[QueueItem], agent_name: str
    ) -> Sequence[AttachmentRecord]:
        """Подмешанные элементы очереди — по вложению на элемент цепочкой,
        роль `user`. Уведомление рядом с сообщением человека получает обёртку,
        которая называет это сообщение настоящим вводом."""
        with_user = False
        for item in items:
            if item.kind is QueueKind.PROMPT:
                with_user = True

        records: list[AttachmentRecord] = []
        for item in items:
            record = self._queued_one(parent, item, agent_name, with_user)
            records.append(record)
            parent = record.uuid

        return records

    def max_turns(
        self, parent: str | None, max_turns: int, turn_count: int
    ) -> AttachmentRecord:
        """Вложение без `rendered`: модели оно не уходит."""
        attachment = MaxTurnsAttachment(max_turns=max_turns, turn_count=turn_count)

        return self._silent(parent, attachment.payload())

    def _queued_one(
        self, parent: str | None, item: QueueItem, agent_name: str, with_user: bool
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

        return AttachmentRecord(
            uuid=self._ids.uuid(),
            parent_uuid=parent,
            timestamp=self._clock.now(),
            session_id=self._session_id,
            version=self._version,
            attachment=attachment.payload(),
            rendered=[RenderedMessage(content=text)],
            rendered_role=AttachmentRole.USER,
        )

    def _system(
        self, parent: str | None, payload: AttachmentPayload, text: str
    ) -> AttachmentRecord:
        wrapped = AgentTag.SYSTEM_REMINDER.wrap_block(text)

        return AttachmentRecord(
            uuid=self._ids.uuid(),
            parent_uuid=parent,
            timestamp=self._clock.now(),
            session_id=self._session_id,
            version=self._version,
            attachment=payload,
            rendered=[RenderedMessage(content=wrapped)],
            rendered_role=AttachmentRole.SYSTEM,
        )

    def _silent(
        self, parent: str | None, payload: AttachmentPayload
    ) -> AttachmentRecord:
        """Запись без `rendered`: только для журнала и сборщика."""
        return AttachmentRecord(
            uuid=self._ids.uuid(),
            parent_uuid=parent,
            timestamp=self._clock.now(),
            session_id=self._session_id,
            version=self._version,
            attachment=payload,
        )
