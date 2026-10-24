"""Вложения контекста: снимок промпта, модель, дата — и свёртка «что объявлено».

Вложение — запись, которой агент сообщает модели что-то помимо слов
пользователя (план, раздел 5.17); текст фиксируется один раз в `rendered`
по шаблонам пакета. Снимок `prompt_snapshot` модели не отправляется: по нему
сборщик берёт системный промпт, инструменты, которые уходят полным
определением, и режим подачи вложений. Свёртка Announced считает по
разговору, что модели уже объявлено. Инструкции серверов MCP идут вложением
`mcp_instructions_delta`, а не системным промптом: смена инструкций посреди
сессии не переписывает начало запроса
(docs/prompt_assembly/texts/attachments/mcp_instructions_delta.md).

Отложенные инструменты (docs/prompt_assembly/specs/07_system_tools.md,
раздел 2.3) объявляются только именами во вложении `deferred_tools_delta`;
модель загружает их инструментом ToolSearch, ссылки на загруженные лежат в
его результатах (`tool_reference`) и в границе сжатия
(`preCompactDiscoveredTools`). Определения загруженных, которые ушли в
запрос, пишутся записью `deferred_tools_record`: по истории запрос
собирается заново байт в байт.

ContextAttachments строит записи, но в сессию их не кладёт: родителя
называет вызывающий. Так сжатие собирает всю пачку — границу, дату,
пересказ, вложения — до того, как первая из них попадёт в сессию.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, TypeVar

from pydantic import TypeAdapter

from boba.agent.ids import Clock, IdMint
from boba.agent.profile import ModelProfile
from boba.agent.queue import QueueItem, QueueKind
from boba.agent.records import (
    AttachmentData,
    AttachmentRecord,
    AttachmentRole,
    ChainRecord,
    CompactBoundaryRecord,
    DateAttachment,
    DeferredToolsDeltaAttachment,
    DeferredToolsRecordAttachment,
    EnvironmentAttachment,
    EnvironmentChange,
    EnvironmentField,
    EnvironmentSnapshot,
    InvokedSkill,
    InvokedSkillsAttachment,
    MaxTurnsAttachment,
    McpInstructionsAttachment,
    ModelAttachment,
    ModelIdentity,
    OriginKind,
    PayloadModel,
    PromptSnapshot,
    QueuedCommandAttachment,
    RenderedMessage,
    SkillListingAttachment,
    SnapshotSkill,
    SnapshotTool,
    ToolResultRecord,
)
from boba.agent.tags import AgentTag
from boba.agent.template import AgentTemplate, AgentTemplateFile
from boba.core.mcp_names import McpToolNames

__all__ = [
    "Announced",
    "ConnectedServer",
    "ContextAttachments",
    "SkillBudget",
    "ToolPoolChange",
]

P = TypeVar("P", bound=PayloadModel)


@dataclass(frozen=True)
class ConnectedServer:
    """Подключённый сервер MCP сессии и его инструкции из server/discover;
    пустая строка — сервер инструкций не дал."""

    name: str
    instructions: str


@dataclass(frozen=True)
class ToolPoolChange:
    """Что изменилось в наборе инструментов относительно объявленного:
    отложенные новые (readded среди них — вернувшиеся), снятые, новые с
    полным определением; search — отложенные после изменения есть."""

    added: Sequence[str]
    removed: Sequence[str]
    readded: frozenset[str]
    surfaced: Sequence[str]
    search: bool

    def empty(self) -> bool:
        if self.added:
            return False

        if self.removed:
            return False

        return not self.surfaced


class ToolWording(StrEnum):
    """Слова текста о снятых и вернувшихся инструментах: с поиском
    инструментов и без него (docs/prompt_assembly/texts/attachments/
    deferred_tools_delta.md, toolSearchAbsent)."""

    TOOL = "tool"
    DEFERRED_TOOL = "deferred tool"
    DO_NOT_CALL = "Do not call them"
    DO_NOT_SEARCH = "Do not search for them — ToolSearch will return no match"
    LOAD_AS_BEFORE = " Load via ToolSearch as before."
    NO_LOAD = ""


@dataclass(frozen=True)
class Wording:
    """Слова одного вложения: вид инструмента, совет о снятых и хвост
    о вернувшихся."""

    kind: ToolWording
    advice: ToolWording
    load: ToolWording


class Announced:
    """Что модели уже объявлено: последние снимок, модель и дата разговора."""

    def __init__(self, conversation: Sequence[ChainRecord]) -> None:
        self._conversation = conversation

    def snapshot(self) -> PromptSnapshot | None:
        return self._last(PromptSnapshot)

    def model_id(self) -> str | None:
        model = self._last(ModelAttachment)
        if model is None:
            return None

        return model.identity.model_id

    def date(self) -> str | None:
        date = self._last(DateAttachment)
        if date is None:
            return None

        return date.date

    def environment(self) -> EnvironmentSnapshot | None:
        environment = self._last(EnvironmentAttachment)
        if environment is None:
            return None

        return environment.snapshot

    def inline_names(self) -> frozenset[str] | None:
        """Инструменты, которые уходят полным определением: последний
        снимок; None — снимка в разговоре нет."""
        snapshot = self.snapshot()
        if snapshot is None:
            return None

        names: set[str] = set()
        for tool in snapshot.tools:
            names.add(tool.name)

        return frozenset(names)

    def deferred_pool(self) -> frozenset[str]:
        """Отложенные инструменты, объявленные модели: свёртка вложений
        deferred_tools_delta разговора."""
        pool: set[str] = set()
        for delta in self._all(DeferredToolsDeltaAttachment):
            pool.update(delta.added_names)
            pool.difference_update(delta.removed_names)

        return frozenset(pool)

    def tool_names(self) -> frozenset[str]:
        """Все инструменты, известные модели: с полным определением и
        отложенные."""
        inline = self.inline_names()
        if inline is None:
            return self.deferred_pool()

        return inline | self.deferred_pool()

    def withdrawn_tools(self) -> frozenset[str]:
        """Имена, которые разговор объявлял и потом снял разницей, а заново
        с тех пор не объявлял."""
        withdrawn: set[str] = set()
        for delta in self._all(DeferredToolsDeltaAttachment):
            withdrawn.difference_update(delta.added_names)
            withdrawn.difference_update(delta.surfaced_names)
            withdrawn.update(delta.removed_names)

        return frozenset(withdrawn)

    def discovered(self) -> Sequence[str]:
        """Загруженные отложенные инструменты в порядке загрузки: список
        границы сжатия, затем ссылки tool_reference в результатах."""
        return list(dict.fromkeys(self._discovered_names()))

    def recorded(self) -> Mapping[str, SnapshotTool]:
        """Определения загруженных отложенных, записанные разговором по
        имени; позднее определение заменяет раннее."""
        definitions: dict[str, SnapshotTool] = {}
        for entries in self._all(DeferredToolsRecordAttachment):
            for tool in entries.entries:
                definitions[tool.name] = tool

        return definitions

    def deferred_loaded(self) -> Sequence[SnapshotTool]:
        """Отложенные инструменты, которые уходят в запрос: загруженные,
        объявленные и записанные, в порядке загрузки."""
        pool = self.deferred_pool()
        definitions = self.recorded()
        loaded: list[SnapshotTool] = []
        for name in self.discovered():
            if name not in pool:
                continue

            tool = definitions.get(name)
            if tool is None:
                continue

            loaded.append(tool)

        return loaded

    def loaded_names(self) -> frozenset[str]:
        """Имена отложенных инструментов, которые уходят в запрос."""
        names: set[str] = set()
        for tool in self.deferred_loaded():
            names.add(tool.name)

        return frozenset(names)

    def deferred_unrecorded(self) -> Sequence[str]:
        """Загруженные и объявленные отложенные, чьих определений разговор
        ещё не записал, в порядке загрузки."""
        pool = self.deferred_pool()
        definitions = self.recorded()
        missing: list[str] = []
        for name in self.discovered():
            if name not in pool:
                continue

            if name in definitions:
                continue

            missing.append(name)

        return missing

    def instructions(self) -> Mapping[str, str]:
        """Объявленные инструкции серверов MCP по имени сервера: свёртка
        вложений mcp_instructions_delta разговора."""
        announced: dict[str, str] = {}
        for delta in self._all(McpInstructionsAttachment):
            texts = zip(delta.added_names, delta.added_server_instructions, strict=True)
            for name, text in texts:
                announced[name] = text

            for name in delta.removed_names:
                announced.pop(name, None)

        return announced

    def skill_names(self) -> frozenset[str]:
        """Имена скиллов, уже перечисленных модели в этом разговоре."""
        names: set[str] = set()
        for listing in self._all(SkillListingAttachment):
            names.update(listing.names)

        return frozenset(names)

    def _discovered_names(self) -> Iterator[str]:
        for record in self._conversation:
            if isinstance(record, CompactBoundaryRecord):
                yield from record.compact_metadata.pre_compact_discovered_tools
                continue

            if not isinstance(record, ToolResultRecord):
                continue

            for result in record.message.tool_results():
                yield from result.references()

    def _all(self, kind: type[P]) -> Iterator[P]:
        """Данные вложений вида kind в порядке разговора."""
        for record in self._conversation:
            if not isinstance(record, AttachmentRecord):
                continue

            if isinstance(record.attachment, kind):
                yield record.attachment

    def _last(self, kind: type[P]) -> P | None:
        for record in reversed(self._conversation):
            if not isinstance(record, AttachmentRecord):
                continue

            if isinstance(record.attachment, kind):
                return record.attachment

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
    SKILLS_JOIN: ClassVar[str] = "\n\n---\n\n"
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
        self._data: TypeAdapter[AttachmentData] = TypeAdapter(AttachmentData)

    def today(self) -> str:
        return self._clock.now()[: self.DATE_LENGTH]

    def snapshot(
        self, parent: str | None, snapshot: PromptSnapshot
    ) -> AttachmentRecord:
        """Снимок без `rendered`: модели он не отправляется."""
        return self._silent(parent, snapshot)

    def model(self, parent: str | None, profile: ModelProfile) -> AttachmentRecord:
        text = self._templates.read(AgentTemplateFile.MODEL_PLAIN).format(
            model_id=profile.model_id
        )
        attachment = ModelAttachment(
            identity=ModelIdentity(model_id=profile.model_id), text=text
        )

        return self._system(parent, attachment, text)

    def date(self, parent: str | None, today: str, changed: bool) -> AttachmentRecord:
        template = AgentTemplateFile.DATE
        if changed:
            template = AgentTemplateFile.DATE_CHANGED

        text = self._templates.read(template).format(date=today)
        attachment = DateAttachment(date=today, changed=changed)

        return self._system(parent, attachment, text)

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

        return self._system(parent, attachment, text)

    def environment_block(
        self, parent: str | None, current: EnvironmentSnapshot
    ) -> AttachmentRecord:
        """Окружение полным блоком: первое объявление и после сжатия."""
        text = self._templates.read(AgentTemplateFile.ENVIRONMENT).format(
            lines=self._environment_lines(current)
        )
        attachment = EnvironmentAttachment(snapshot=current)

        return self._system(parent, attachment, text)

    def deferred_delta(
        self, parent: str | None, change: ToolPoolChange
    ) -> AttachmentRecord:
        """Разница набора инструментов одними именами (docs/prompt_assembly,
        deferred_tools_delta): новые с полным определением — «стали
        доступны»; новые отложенные — списком для ToolSearch; вернувшиеся —
        числом и именами, инструменты MCP — как вернувшиеся с сервером;
        снятые — по имени, инструменты MCP — как ушедшие с сервером."""
        lines: list[str] = []
        for name in change.added:
            if name in change.readded:
                continue

            lines.append(name)

        wording = self._wording(change.search)
        sections: list[str] = []
        if change.surfaced:
            sections.append(
                self._templates.read(AgentTemplateFile.TOOLS_SURFACED).format(
                    names="\n".join(change.surfaced)
                )
            )

        if lines:
            sections.append(
                self._templates.read(AgentTemplateFile.DEFERRED_ADDED).format(
                    names="\n".join(lines)
                )
            )

        returned = sorted(change.readded)
        sections.extend(self._readded(returned, wording))
        sections.extend(self._removed(change.removed, wording))

        attachment = DeferredToolsDeltaAttachment(
            added_names=list(change.added),
            added_lines=lines,
            removed_names=list(change.removed),
            readded_names=returned,
            surfaced_names=list(change.surfaced),
            tool_search_absent=not change.search,
        )

        return self._system(parent, attachment, "\n\n".join(sections))

    def deferred_record(
        self, parent: str | None, entries: Sequence[SnapshotTool]
    ) -> AttachmentRecord:
        """Определения загруженных отложенных инструментов без `rendered`:
        модели запись не отправляется, по ней собирается запрос."""
        attachment = DeferredToolsRecordAttachment(entries=entries)

        return self._silent(parent, attachment)

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

        return self._system(parent, attachment, "\n\n".join(sections))

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

    def _readded(self, names: Sequence[str], wording: Wording) -> Iterator[str]:
        """Разделы о вернувшихся инструментах: сначала инструменты MCP, затем
        прочие."""
        servers = list(self._picked(names, True))
        own = list(self._picked(names, False))
        if servers:
            yield self._templates.read(AgentTemplateFile.TOOLS_READDED_MCP).format(
                subject=self._counted(len(servers), wording.kind),
                names=self._grouped(servers),
                load=wording.load,
            )

        if own:
            yield self._templates.read(AgentTemplateFile.TOOLS_READDED).format(
                subject=self._counted(len(own), wording.kind),
                names=", ".join(own),
                load=wording.load,
            )

    def _removed(self, names: Sequence[str], wording: Wording) -> Iterator[str]:
        """Разделы о снятых инструментах и замыкающая строка о фоновом
        контексте; без снятых — ничего."""
        servers = list(self._picked(names, True))
        own = list(self._picked(names, False))
        if len(servers) > self.LISTED_UP_TO:
            yield self._templates.read(AgentTemplateFile.TOOLS_REMOVED_MCP_MANY).format(
                count=len(servers),
                kind=wording.kind,
                names=self._grouped(servers),
                advice=wording.advice,
            )
        elif servers:
            yield self._templates.read(AgentTemplateFile.TOOLS_REMOVED_MCP).format(
                kind=wording.kind, names="\n".join(servers), advice=wording.advice
            )

        if own:
            yield self._templates.read(AgentTemplateFile.TOOLS_REMOVED).format(
                kind=wording.kind, names="\n".join(own), advice=wording.advice
            )

        if names:
            yield self._templates.read(AgentTemplateFile.AMBIENT)

    @staticmethod
    def _wording(search: bool) -> Wording:
        """Слова разделов: с ToolSearch речь об отложенных инструментах."""
        if search:
            return Wording(
                kind=ToolWording.DEFERRED_TOOL,
                advice=ToolWording.DO_NOT_SEARCH,
                load=ToolWording.LOAD_AS_BEFORE,
            )

        return Wording(
            kind=ToolWording.TOOL,
            advice=ToolWording.DO_NOT_CALL,
            load=ToolWording.NO_LOAD,
        )

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
    def _counted(count: int, kind: ToolWording) -> str:
        if count == 1:
            return f"1 {kind} is"

        return f"{count} {kind}s are"

    def invoked_skills(
        self, parent: str | None, skills: Sequence[InvokedSkill]
    ) -> AttachmentRecord:
        """Скиллы, вызванные до сжатия: имя и текст каждого блоком."""
        blocks: list[str] = []
        for skill in skills:
            blocks.append(
                self._templates.read(AgentTemplateFile.INVOKED_SKILL).format(
                    name=skill.name, content=skill.content
                )
            )

        text = self._templates.read(AgentTemplateFile.INVOKED_SKILLS).format(
            skills=self.SKILLS_JOIN.join(blocks)
        )
        attachment = InvokedSkillsAttachment(skills=skills)

        return self._system(parent, attachment, text)

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

        return self._system(parent, attachment, text)

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

        return self._silent(parent, attachment)

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
            attachment=self._written(attachment),
            rendered=[RenderedMessage(content=text)],
            rendered_role=AttachmentRole.USER,
        )

    def _system(
        self, parent: str | None, payload: AttachmentData, text: str
    ) -> AttachmentRecord:
        wrapped = AgentTag.SYSTEM_REMINDER.wrap_block(text)

        return AttachmentRecord(
            uuid=self._ids.uuid(),
            parent_uuid=parent,
            timestamp=self._clock.now(),
            session_id=self._session_id,
            version=self._version,
            attachment=self._written(payload),
            rendered=[RenderedMessage(content=wrapped)],
            rendered_role=AttachmentRole.SYSTEM,
        )

    def _silent(self, parent: str | None, payload: AttachmentData) -> AttachmentRecord:
        """Запись без `rendered`: только для журнала и сборщика."""
        return AttachmentRecord(
            uuid=self._ids.uuid(),
            parent_uuid=parent,
            timestamp=self._clock.now(),
            session_id=self._session_id,
            version=self._version,
            attachment=self._written(payload),
        )

    def _written(self, payload: AttachmentData) -> AttachmentData:
        """Данные вложения в том виде, в каком их прочитает загрузчик из
        журнала: запись в памяти равна записи после перезапуска."""
        document = payload.model_dump(by_alias=True, mode="json")

        return self._data.validate_python(document)
