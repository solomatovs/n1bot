"""Сжатие разговора: заполненность, решение, пересказ и записи после него.

Устройство — план, раздел 5.16 (разбор в docs/prompt_assembly/specs/13).
Заполненность считает ContextMeter: учёт последнего ответа модели плюс
оценка всего, что записано после него; без учёта — оценка всего разговора с
системным промптом и инструментами. CompactGate сравнивает её с порогом и
пределом и ведёт предохранители хода (CompactTracking). RoundGrouper режет
разговор на группы по ответам модели; Summarizer пересказывает все группы,
кроме последней, тем же системным промптом и инструментами, что основной
запрос, и при переполнении окна отбрасывает голову — лестница. Compactor
собирает пачку записей после сжатия: границу, дату, пересказ и вложения
контекста заново; хвост (последняя группа) переносится без записи.

Ошибки наружу не выходят: неудача пересказа — значение NotSummarized,
остановка хода — SummaryAborted.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

import mcp_types as mt

from boba.agent.asking import (
    Aborted,
    Errored,
    ModelAsk,
    OutputLimited,
    Overflowed,
)
from boba.agent.attachments import (
    Announced,
    AttachmentKind,
    ContextAttachments,
    DeferredToolsRecordAttachment,
    EnvironmentSnapshot,
    InvokedSkill,
    InvokedSkillsAttachment,
    PromptSnapshot,
    SkillBudget,
    SnapshotSkill,
    SnapshotTool,
    ToolPoolChange,
)
from boba.agent.builder import RequestBuilder
from boba.agent.ids import Clock, IdMint
from boba.agent.profile import CompactionSettings, ModelProfile
from boba.agent.records import (
    AssistantRecord,
    AttachmentRecord,
    ChainRecord,
    CompactBoundaryRecord,
    CompactMetadata,
    CompactTrigger,
    LinkText,
    PreservedMessages,
    Record,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    UserRecord,
)
from boba.agent.session import Session
from boba.agent.skills import SkillCallInput, SkillTool
from boba.agent.tags import ReplyTag
from boba.agent.template import AgentTemplate, AgentTemplateFile
from boba.agent.tools import AgentTools
from boba.agent.writer import RequestRecorder
from boba.cancellation import RunCancellation, StopReason
from boba.llm.chat import ChatReply, ChatRequest

logger = logging.getLogger(__name__)

__all__ = [
    "CompactGate",
    "CompactTracking",
    "Compactor",
    "ContextMeter",
    "Fill",
    "FillLevel",
    "GateDecision",
    "InvokedSkills",
    "NotSummarized",
    "RoundGrouper",
    "Summarized",
    "Summarizer",
    "SummaryAborted",
    "SummaryFailure",
    "TokenEstimator",
]


class TokenEstimator:
    """Оценка записей в токенах без провайдера: знаки, делённые на
    `chars_per_token` профиля; вызов инструмента — имя и JSON аргументов,
    вложение — его `rendered`, определения загруженных отложенных
    инструментов (deferred_tools_record) — как описание инструмента в
    запросе, прочие записи только для журнала — ноль."""

    def __init__(self, profile: ModelProfile) -> None:
        self._profile = profile

    def text(self, text: str) -> int:
        return self._profile.estimate(text)

    def texts(self, texts: Sequence[str]) -> int:
        total = 0
        for text in texts:
            total += self.text(text)

        return total

    def record(self, record: ChainRecord) -> int:
        return self.texts(list(self._texts_of(record)))

    def records(self, records: Sequence[ChainRecord]) -> int:
        total = 0
        for record in records:
            total += self.record(record)

        return total

    def prefix(self, snapshot: PromptSnapshot) -> int:
        """Системный промпт и описания инструментов снимка."""
        texts: list[str] = list(snapshot.system_prompt)
        texts.extend(self._tool_texts(snapshot.tools))

        return self.texts(texts)

    @staticmethod
    def _tool_texts(tools: Sequence[SnapshotTool]) -> Iterator[str]:
        for tool in tools:
            yield tool.text()
            yield json.dumps(tool.schema_, ensure_ascii=False)

    def _texts_of(self, record: ChainRecord) -> Iterator[str]:
        if isinstance(record, UserRecord):
            yield from self._user_texts(record)
            return

        if isinstance(record, AssistantRecord):
            if record.is_api_error_message:
                return

            yield from self._assistant_texts(record)
            return

        if isinstance(record, AttachmentRecord):
            yield from self._attachment_texts(record)

    def _attachment_texts(self, record: AttachmentRecord) -> Iterator[str]:
        if record.attachment.type == AttachmentKind.DEFERRED_TOOLS_RECORD.value:
            entries = DeferredToolsRecordAttachment.model_validate(
                record.attachment.model_dump()
            )
            yield from self._tool_texts(entries.entries)
            return

        if record.rendered is None:
            return

        for message in record.rendered:
            yield message.content

    @staticmethod
    def _user_texts(record: UserRecord) -> Iterator[str]:
        content = record.message.content
        if isinstance(content, str):
            yield content
            return

        for block in content:
            if isinstance(block, ToolResultBlock):
                yield from block.references()
                yield block.text()
                continue

            if isinstance(block, mt.ResourceLink):
                yield LinkText.TEMPLATE.render(block)
                continue

            yield block.text

    @staticmethod
    def _assistant_texts(record: AssistantRecord) -> Iterator[str]:
        for block in record.message.content:
            if isinstance(block, ThinkingBlock):
                yield block.thinking
                continue

            if isinstance(block, TextBlock):
                yield block.text
                continue

            if isinstance(block, ToolUseBlock):
                yield block.name
                yield json.dumps(block.input, ensure_ascii=False)


@dataclass(frozen=True)
class Anchor:
    """Откуда считается заполненность: учёт и позиция в разговоре."""

    tokens: int
    index: int


class ContextMeter:
    """Заполненность разговора в токенах.

    Якорь — последний ответ модели с ненулевым учётом (не синтетический;
    перенесённый хвост несёт нули и якорем не бывает), сдвинутый к первой
    записи того же ответа; его учёт уже включает системный промпт и
    инструменты. Граница сжатия в начале разговора — якорь без учёта: к
    оценке записей после неё прибавляется оценка системного промпта и
    инструментов снимка, иначе заполненность сразу после сжатия занижена на
    них. Граница посреди разговора — след сжатия, упавшего между записями
    (план, раздел 5.16): она не якорь. К учёту якоря прибавляется оценка
    записей после него. Без якоря оценивается весь разговор вместе с
    системным промптом и инструментами снимка.
    """

    def __init__(self, estimator: TokenEstimator) -> None:
        self._estimator = estimator

    def tokens(
        self, conversation: Sequence[ChainRecord], snapshot: PromptSnapshot
    ) -> int:
        anchor = self._anchor(conversation, snapshot)
        if anchor is None:
            return self._estimator.prefix(snapshot) + self._estimator.records(
                conversation
            )

        return anchor.tokens + self._estimator.records(conversation[anchor.index + 1 :])

    def _anchor(
        self, conversation: Sequence[ChainRecord], snapshot: PromptSnapshot
    ) -> Anchor | None:
        for index in range(len(conversation) - 1, -1, -1):
            record = conversation[index]
            if isinstance(record, CompactBoundaryRecord):
                if index == 0:
                    return Anchor(tokens=self._estimator.prefix(snapshot), index=index)

                continue

            if not isinstance(record, AssistantRecord):
                continue

            if record.is_synthetic():
                continue

            usage = record.message.usage
            if usage.is_zero():
                continue

            tokens = (
                usage.input_tokens
                + usage.output_tokens
                + usage.cache_creation_input_tokens
                + usage.cache_read_input_tokens
            )

            return Anchor(
                tokens=tokens, index=self._first_of_reply(conversation, index)
            )

        return None

    @staticmethod
    def _first_of_reply(conversation: Sequence[ChainRecord], index: int) -> int:
        """Первая запись ответа с тем же `message.id`, что у записи index."""
        record = conversation[index]
        if not isinstance(record, AssistantRecord):
            return index

        message_id = record.message.id
        first = index
        for earlier in range(index - 1, -1, -1):
            candidate = conversation[earlier]
            if not isinstance(candidate, AssistantRecord):
                break

            if candidate.message.id != message_id:
                break

            first = earlier

        return first


class FillLevel(StrEnum):
    """Что заполненность значит для хода."""

    OK = "ok"
    COMPACT = "compact"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Fill:
    tokens: int
    level: FillLevel

    @property
    def needs_compaction(self) -> bool:
        return self.level is not FillLevel.OK


@dataclass
class CompactTracking:
    """Предохранители сжатия; живут один ход (план, раздел 5.16)."""

    FAILURES_TO_OPEN: ClassVar[int] = 3
    RAPID_WINDOW: ClassVar[int] = 3
    RAPID_TRIPS: ClassVar[int] = 3

    compacted: bool = False
    turn_counter: int = 0
    consecutive_failures: int = 0
    consecutive_rapid_refills: int = 0
    last_failure: str = ""

    @property
    def breaker_open(self) -> bool:
        return self.consecutive_failures >= self.FAILURES_TO_OPEN

    def refill_count(self) -> int:
        """Сколько раз подряд заполненность вернулась за порог быстро после сжатия."""
        if self.compacted and self.turn_counter < self.RAPID_WINDOW:
            return self.consecutive_rapid_refills + 1

        return 0

    def tripped(self) -> bool:
        return self.refill_count() >= self.RAPID_TRIPS

    def succeeded(self) -> None:
        self.consecutive_rapid_refills = self.refill_count()
        self.compacted = True
        self.turn_counter = 0
        self.consecutive_failures = 0
        self.last_failure = ""

    def failed(self, detail: str) -> None:
        self.consecutive_failures += 1
        self.last_failure = detail

    def tools_ran(self) -> None:
        self.turn_counter += 1


class GateDecision(StrEnum):
    NOT_NEEDED = "not_needed"
    BREAKER_OPEN = "breaker_open"
    RAPID_REFILL = "rapid_refill"
    COMPACT = "compact"


class CompactGate:
    """Пора ли сжимать: заполненность против порога и предела с учётом
    предохранителей хода. Создаёт цикл хода."""

    def __init__(
        self,
        meter: ContextMeter,
        settings: CompactionSettings,
        profile: ModelProfile,
        instruction: str,
    ) -> None:
        self._meter = meter
        self._threshold = settings.threshold(profile, instruction)
        self._limit = settings.limit(profile)

    def measure(
        self, conversation: Sequence[ChainRecord], snapshot: PromptSnapshot
    ) -> Fill:
        tokens = self._meter.tokens(conversation, snapshot)
        if tokens >= self._limit:
            return Fill(tokens, FillLevel.BLOCKED)

        if tokens >= self._threshold:
            return Fill(tokens, FillLevel.COMPACT)

        return Fill(tokens, FillLevel.OK)

    def decide(self, fill: Fill, tracking: CompactTracking) -> GateDecision:
        if tracking.breaker_open:
            return GateDecision.BREAKER_OPEN

        if not fill.needs_compaction:
            return GateDecision.NOT_NEEDED

        if tracking.tripped():
            return GateDecision.RAPID_REFILL

        return GateDecision.COMPACT


class RoundGrouper:
    """Группы разговора: новая группа начинается на ответе модели с новым
    `message.id`; всё до первого ответа — начальная группа. Синтетические
    ответы группу не открывают."""

    def groups(self, conversation: Sequence[ChainRecord]) -> list[list[ChainRecord]]:
        groups: list[list[ChainRecord]] = []
        current: list[ChainRecord] = []
        message_id = ""
        for record in conversation:
            if self._opens_group(record, message_id):
                if current:
                    groups.append(current)

                current = []
                if isinstance(record, AssistantRecord):
                    message_id = record.message.id

            current.append(record)

        if current:
            groups.append(current)

        return groups

    @staticmethod
    def _opens_group(record: ChainRecord, message_id: str) -> bool:
        if not isinstance(record, AssistantRecord):
            return False

        if record.is_synthetic():
            return False

        return record.message.id != message_id

    @staticmethod
    def has_reply(records: Sequence[ChainRecord]) -> bool:
        for record in records:
            if isinstance(record, AssistantRecord) and not record.is_synthetic():
                return True

        return False


class SummaryFailure(StrEnum):
    """Почему пересказ не состоялся; текст — для лога и записи о пределе."""

    TOO_FEW_GROUPS = "too_few_groups"
    TOOL_CALLS = "tool_calls"
    NO_BLOCK = "no_summary_block"
    EMPTY = "empty_summary"
    OUTPUT_LIMIT = "output_limit"
    TOO_LONG = "summary_longer_than_source"
    MODEL_ERROR = "model_error"
    REQUEST_TOO_LONG = "request_too_long"

    def describe(self, detail: str) -> str:
        texts = {
            SummaryFailure.TOO_FEW_GROUPS: (
                "there is nothing to summarize yet: the conversation holds fewer "
                "than two rounds with a model reply"
            ),
            SummaryFailure.TOOL_CALLS: (
                "the model answered the summary request with tool calls instead of text"
            ),
            SummaryFailure.NO_BLOCK: (
                "the summary reply has no <summary> block to take the summary from"
            ),
            SummaryFailure.EMPTY: "the summary reply is empty",
            SummaryFailure.OUTPUT_LIMIT: (
                "the summary reply was cut by the output limit of the model"
            ),
            SummaryFailure.TOO_LONG: (
                "the summary is longer than the part of the conversation it replaces"
            ),
            SummaryFailure.MODEL_ERROR: "the summary request failed",
            SummaryFailure.REQUEST_TOO_LONG: (
                "the summary request does not fit the context window even after "
                "dropping the oldest rounds"
            ),
        }
        text = texts[self]
        if detail:
            return f"{text}: {detail}"

        return text


@dataclass(frozen=True)
class Summarized:
    """Пересказ получен: текст, что пересказано и что переносится хвостом;
    head_truncated — лестница отбросила старшие группы, и пересказ их не
    покрывает."""

    text: str
    summarized: Sequence[ChainRecord]
    kept: Sequence[ChainRecord]
    head_truncated: bool


@dataclass(frozen=True)
class NotSummarized:
    reason: SummaryFailure
    detail: str

    def describe(self) -> str:
        return self.reason.describe(self.detail)


@dataclass(frozen=True)
class SummaryAborted:
    reason: StopReason


SummaryOutcome = Summarized | NotSummarized | SummaryAborted


class Summarizer:
    """Запрос пересказа и разбор ответа.

    Пересказывается всё, кроме последней группы; запрос несёт тот же
    системный промпт и инструменты, что основной, плюс инструкцию отдельным
    сообщением пользователя (сборщик сливает её с последним текстом
    пользователя, когда отрезок кончается им). Переполнение окна на запросе
    пересказа отбрасывает старшую группу и повторяет попытку, не больше
    LADDER_STEPS раз; остаток, который теперь начинается ответом модели,
    открывается пометкой об усечении, а пересказ — оговоркой, что начало
    разговора он не покрывает (docs/prompt_assembly/specs/04_compaction.md,
    2.1–2.2). Создаёт цикл хода.
    """

    LADDER_STEPS: ClassVar[int] = 3

    def __init__(  # noqa: PLR0913 — пересказ собирается из частей ядра, как цикл хода
        self,
        ask: ModelAsk,
        builder: RequestBuilder,
        templates: AgentTemplate,
        estimator: TokenEstimator,
        ids: IdMint,
        clock: Clock,
        version: str,
        recorder: RequestRecorder,
    ) -> None:
        self._recorder = recorder
        self._ask = ask
        self._builder = builder
        self._templates = templates
        self._estimator = estimator
        self._grouper = RoundGrouper()
        self._ids = ids
        self._clock = clock
        self._version = version

    async def summarize(
        self,
        session: Session,
        conversation: Sequence[ChainRecord],
        snapshot: PromptSnapshot,
        cancellation: RunCancellation,
    ) -> SummaryOutcome:
        groups = self._grouper.groups(conversation)
        if len(groups) < 2:  # noqa: PLR2004 — начальная группа и хотя бы один ответ
            return NotSummarized(
                SummaryFailure.TOO_FEW_GROUPS, f"{len(groups)} group(s) in total"
            )

        kept = groups[-1]
        overflow = ""
        for dropped in range(self.LADDER_STEPS + 1):
            head = self._flat(groups[dropped:-1])
            if not self._grouper.has_reply(head):
                return self._nothing_left(dropped, overflow)

            request = self._request(session, head, conversation, snapshot, dropped)
            outcome = await self._attempt(request, head, cancellation)
            if isinstance(outcome, str):
                return Summarized(
                    text=outcome, summarized=head, kept=kept, head_truncated=dropped > 0
                )

            if not isinstance(outcome, Overflowed):
                return outcome

            overflow = outcome.error
            logger.info(
                "compaction: the summary request overflowed, dropping the oldest "
                "round (%d of %d)",
                dropped + 1,
                self.LADDER_STEPS,
            )

        return NotSummarized(SummaryFailure.REQUEST_TOO_LONG, overflow)

    @staticmethod
    def _nothing_left(dropped: int, overflow: str) -> NotSummarized:
        """В отрезке не осталось ответа модели: сразу — пересказывать нечего,
        после ступеней лестницы — запрос не влез и отбрасывать больше нечего."""
        if not dropped:
            return NotSummarized(
                SummaryFailure.TOO_FEW_GROUPS,
                "the part to summarize holds no model reply",
            )

        return NotSummarized(
            SummaryFailure.REQUEST_TOO_LONG,
            f"{overflow} (after dropping {dropped} oldest round(s) no model reply "
            "is left to summarize)",
        )

    def _request(
        self,
        session: Session,
        head: Sequence[ChainRecord],
        conversation: Sequence[ChainRecord],
        snapshot: PromptSnapshot,
        dropped: int,
    ) -> ChatRequest:
        """Запрос пересказа отрезка; после отброшенных групп отрезок
        начинается ответом модели, и перед ним встаёт пометка об усечении."""
        sent: list[ChainRecord] = []
        if dropped:
            sent.append(self._user(session, AgentTemplateFile.TRUNCATED_FOR_RETRY))

        sent.extend(head)
        sent.append(self._user(session, AgentTemplateFile.SUMMARIZE_INSTRUCTION))

        return self._builder.summary(sent, conversation, snapshot)

    async def _attempt(
        self,
        request: ChatRequest,
        head: Sequence[ChainRecord],
        cancellation: RunCancellation,
    ) -> str | NotSummarized | SummaryAborted | Overflowed:
        """Один запрос пересказа: текст пересказа либо неудача; Overflowed —
        ступень лестницы."""
        await self._recorder.record(request)
        asked = await self._ask.attempt(
            request, cancellation, show=False, message_id=""
        )
        if isinstance(asked, Aborted):
            return SummaryAborted(asked.reason)

        if isinstance(asked, Overflowed):
            return asked

        if isinstance(asked, OutputLimited):
            return NotSummarized(SummaryFailure.OUTPUT_LIMIT, asked.error)

        if isinstance(asked, Errored):
            return NotSummarized(SummaryFailure.MODEL_ERROR, asked.error)

        return self._parsed(asked.reply, head)

    def _parsed(
        self, reply: ChatReply, head: Sequence[ChainRecord]
    ) -> str | NotSummarized:
        if reply.tool_calls:
            names = [call.name for call in reply.tool_calls]
            return NotSummarized(SummaryFailure.TOOL_CALLS, ", ".join(names))

        if not reply.content.strip():
            return NotSummarized(SummaryFailure.EMPTY, "")

        # разбор <analysis> вырезается первым: модель упоминает в нём тег
        # <summary> словами, и первым блоком оказался бы кусок разбора
        reply_text = ReplyTag.ANALYSIS.without(reply.content)
        text = ReplyTag.SUMMARY.extract(reply_text)
        if text is None:
            return NotSummarized(SummaryFailure.NO_BLOCK, reply.content[:200])

        text = text.strip()
        if not text:
            return NotSummarized(SummaryFailure.EMPTY, "the <summary> block is empty")

        summary_tokens = self._estimator.text(text)
        source_tokens = self._estimator.records(head)
        if summary_tokens > source_tokens:
            return NotSummarized(
                SummaryFailure.TOO_LONG,
                f"about {summary_tokens} tokens against {source_tokens}",
            )

        return text

    def _user(self, session: Session, template: AgentTemplateFile) -> UserRecord:
        """Текст запроса пересказа — инструкция или пометка об усечении — как
        запись пользователя для сборщика; в сессию не попадает."""
        text = self._templates.read(template)

        return UserRecord(
            uuid=self._ids.uuid(),
            parent_uuid=None,
            timestamp=self._clock.now(),
            session_id=session.id.value,
            version=self._version,
            message=UserMessage(content=text),
        )

    @staticmethod
    def _flat(groups: Sequence[Sequence[ChainRecord]]) -> list[ChainRecord]:
        flat: list[ChainRecord] = []
        for group in groups:
            flat.extend(group)

        return flat


class InvokedSkills:
    """Скиллы, вызванные в пересказанной части разговора, для вложения
    invoked_skills: модель после границы не видит их текстов, а их правила
    могут ещё действовать (docs/prompt_assembly/specs/04_compaction.md, 3.3).

    Текст скилла — результат его вызова, как модель его получила; вложение
    invoked_skills прежнего сжатия переносится дальше. Новые первыми, каждый
    и все вместе — в пределах бюджета: 5000 и 25000 токенов по разбору на
    окне от 200 000, на меньшем окне — та же доля окна. Создаёт Compactor.
    """

    PER_SKILL_TOKENS: ClassVar[int] = 5_000
    TOTAL_TOKENS: ClassVar[int] = 25_000
    FULL_WINDOW: ClassVar[int] = 200_000

    def __init__(self, profile: ModelProfile) -> None:
        share = min(1.0, profile.context_window / self.FULL_WINDOW)
        self._per_skill = int(self.PER_SKILL_TOKENS * share) * profile.chars_per_token
        self._total = int(self.TOTAL_TOKENS * share) * profile.chars_per_token

    def of(self, summarized: Sequence[ChainRecord]) -> Sequence[InvokedSkill]:
        latest: dict[str, str] = {}
        for name, content in self._invocations(summarized):
            latest.pop(name, None)
            latest[name] = content

        newest = list(latest.items())
        newest.reverse()

        return list(self._bounded(newest))

    def _bounded(self, newest: Sequence[tuple[str, str]]) -> Iterator[InvokedSkill]:
        left = self._total
        for name, content in newest:
            size = min(self._per_skill, left)
            text = content[:size]
            if not text:
                return

            left -= len(text)
            yield InvokedSkill(name=name, content=text)

    def _invocations(self, records: Sequence[ChainRecord]) -> Iterator[tuple[str, str]]:
        """Вызовы скиллов по порядку: прежнее вложение и удачные вызовы Skill."""
        called: dict[str, str] = {}
        for record in records:
            if isinstance(record, AttachmentRecord):
                yield from self._carried(record)
                continue

            if isinstance(record, AssistantRecord):
                called.update(self._calls(record))
                continue

            if isinstance(record, UserRecord):
                yield from self._results(record, called)

    @staticmethod
    def _carried(record: AttachmentRecord) -> Iterator[tuple[str, str]]:
        if record.attachment.type != AttachmentKind.INVOKED_SKILLS.value:
            return

        attachment = InvokedSkillsAttachment.model_validate(
            record.attachment.model_dump()
        )
        for skill in reversed(attachment.skills):
            yield skill.name, skill.content

    @staticmethod
    def _calls(record: AssistantRecord) -> Iterator[tuple[str, str]]:
        """Вызовы Skill ответа: id вызова → имя скилла."""
        for block in record.message.tool_uses():
            if block.name != SkillTool.NAME:
                continue

            yield block.id, SkillCallInput.model_validate(block.input).skill

    @staticmethod
    def _results(
        record: UserRecord, called: dict[str, str]
    ) -> Iterator[tuple[str, str]]:
        for result in record.message.tool_results():
            name = called.get(result.tool_use_id)
            if name is None:
                continue

            if result.is_error:
                continue

            yield name, result.text()


class Compactor:
    """Пачка записей после сжатия (план, раздел 5.16): граница со списком
    загруженных отложенных инструментов, дата, пересказ, затем вложения в
    порядке docs/prompt_assembly/specs/13_compaction_model.md, 1.6 —
    вызванные скиллы, отложенные инструменты, инструкции серверов MCP,
    окружение, модель — и список скиллов; в сессию разом, без точек
    ожидания между ними, чтобы писатель положил их одним `append`. Хвост не
    переписывается: граница называет его записи по uuid. Создаёт цикл хода."""

    def __init__(  # noqa: PLR0913 — пачка собирается из всех объявлений контекста
        self,
        attachments: ContextAttachments,
        estimator: TokenEstimator,
        templates: AgentTemplate,
        ids: IdMint,
        clock: Clock,
        version: str,
        profile: ModelProfile,
        environment: EnvironmentSnapshot | None,
        skills: Sequence[SnapshotSkill],
        budget: SkillBudget,
        catalog: AgentTools,
    ) -> None:
        self._attachments = attachments
        self._estimator = estimator
        self._templates = templates
        self._ids = ids
        self._clock = clock
        self._version = version
        self._profile = profile
        self._environment = environment
        self._skills = list(skills)
        self._budget = budget
        self._catalog = catalog
        self._invoked = InvokedSkills(profile)

    def records(
        self,
        session: Session,
        summary: Summarized,
        pre_tokens: int,
        duration_ms: int,
        today: str,
    ) -> Sequence[Record]:
        """Записи пачки с seq; логический родитель границы — последняя
        запись разговора, то есть последняя запись хвоста."""
        discovered = Announced(session.conversation()).discovered()
        boundary_uuid = self._ids.uuid()
        date = self._attachments.date(boundary_uuid, today, False)
        summary_record = self._summary_record(session, date.uuid, summary)
        inputs = self._inputs(summary_record.uuid, summary.summarized)
        post_tokens = self._estimator.records(
            [date, summary_record, *summary.kept, *inputs]
        )
        kept_uuids = [record.uuid for record in summary.kept]
        boundary = CompactBoundaryRecord(
            uuid=boundary_uuid,
            parent_uuid=None,
            timestamp=self._clock.now(),
            session_id=session.id.value,
            version=self._version,
            logical_parent_uuid=summary.kept[-1].uuid,
            compact_metadata=CompactMetadata(
                trigger=CompactTrigger.AUTO,
                pre_tokens=pre_tokens,
                post_tokens=post_tokens,
                duration_ms=duration_ms,
                preserved_messages=PreservedMessages(
                    anchor_uuid=summary_record.uuid,
                    uuids=kept_uuids,
                    all_uuids=kept_uuids,
                ),
                pre_compact_discovered_tools=discovered,
            ),
        )

        added: list[Record] = []
        for record in [boundary, date, summary_record, *inputs]:
            added.append(session.add(record))

        return added

    def _summary_record(
        self, session: Session, parent: str, summary: Summarized
    ) -> UserRecord:
        """Пересказ в обёртке; без начала разговора — с оговоркой об этом."""
        text = summary.text
        if summary.head_truncated:
            text = self._templates.read(
                AgentTemplateFile.SUMMARY_HEAD_TRUNCATED
            ).format(summary=text)

        wrapped = self._templates.read(AgentTemplateFile.SUMMARY_WRAPPER).format(
            summary=text
        )

        return UserRecord(
            uuid=self._ids.uuid(),
            parent_uuid=parent,
            timestamp=self._clock.now(),
            session_id=session.id.value,
            version=self._version,
            message=UserMessage(content=wrapped),
            is_compact_summary=True,
            is_visible_in_transcript_only=True,
        )

    def _inputs(
        self, parent: str, summarized: Sequence[ChainRecord]
    ) -> list[AttachmentRecord]:
        """Вложения контекста заново: модель прежних не видит."""
        records: list[AttachmentRecord] = []
        if invoked := self._invoked.of(summarized):
            records.append(self._attachments.invoked_skills(parent, invoked))
            parent = records[-1].uuid

        deferred: list[str] = []
        for tool in self._catalog.deferred():
            deferred.append(tool.name)

        if deferred:
            change = ToolPoolChange(
                added=sorted(deferred),
                removed=(),
                readded=frozenset(),
                surfaced=(),
                search=True,
            )
            records.append(self._attachments.deferred_delta(parent, change))
            parent = records[-1].uuid

        instructions = self._attachments.instructions(
            parent, self._catalog.connected(), {}
        )
        if instructions is not None:
            records.append(instructions)
            parent = instructions.uuid

        if self._environment is not None:
            records.append(
                self._attachments.environment_block(parent, self._environment)
            )
            parent = records[-1].uuid

        records.append(self._attachments.model(parent, self._profile))
        parent = records[-1].uuid

        if self._skills:
            records.append(
                self._attachments.skill_listing(
                    parent, self._skills, self._budget, True
                )
            )

        return records
