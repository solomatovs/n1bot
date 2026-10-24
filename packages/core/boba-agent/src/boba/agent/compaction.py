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
    ContextAttachments,
    SkillBudget,
    ToolPoolChange,
)
from boba.agent.builder import RequestBuilder
from boba.agent.ids import Clock, IdMint
from boba.agent.profile import CompactionSettings, ModelProfile
from boba.agent.records import (
    ApiFailureRecord,
    AssistantRecord,
    AttachmentRecord,
    ChainRecord,
    CompactBoundaryRecord,
    CompactMetadata,
    CompactSummaryRecord,
    CompactTrigger,
    DeferredToolsRecordAttachment,
    EnvironmentSnapshot,
    InvokedSkill,
    InvokedSkillsAttachment,
    LinkText,
    MetaRecord,
    PreservedMessages,
    PromptSnapshot,
    Record,
    SnapshotSkill,
    SnapshotTool,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolResultRecord,
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
from boba.llm.chat import ChatReply, ChatRequest, ContextOverflowSigns

logger = logging.getLogger(__name__)

__all__ = [
    "CompactGate",
    "CompactJob",
    "CompactPack",
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
    "SummaryInstruction",
    "SummaryJob",
    "SummaryPressure",
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

        if isinstance(record, ApiFailureRecord):
            return

        if isinstance(record, AssistantRecord):
            yield from self._assistant_texts(record)
            return

        if isinstance(record, AttachmentRecord):
            yield from self._attachment_texts(record)

    def _attachment_texts(self, record: AttachmentRecord) -> Iterator[str]:
        if isinstance(record.attachment, DeferredToolsRecordAttachment):
            yield from self._tool_texts(record.attachment.entries)
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
    last_reason: str = ""
    """Причина последней неудачи без текста нижнего слоя: для человека."""
    last_detail: str = ""
    exhausted: bool = False
    """Последняя неудача — разговор не помещается в окно и после сжатия."""

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
        self.last_reason = ""
        self.last_detail = ""
        self.exhausted = False

    def failed(self, failure: NotSummarized) -> None:
        self.consecutive_failures += 1
        self.last_failure = failure.describe()
        self.last_reason = failure.brief()
        self.last_detail = failure.detail
        self.exhausted = failure.reason is SummaryFailure.DOES_NOT_FIT

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
    EMPTY = "empty_summary"
    OUTPUT_LIMIT = "output_limit"
    DOES_NOT_FIT = "does_not_fit"
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
            SummaryFailure.EMPTY: "the summary reply is empty",
            SummaryFailure.OUTPUT_LIMIT: (
                "the summary reply was cut by the output limit of the model"
            ),
            SummaryFailure.DOES_NOT_FIT: (
                "the conversation does not fit the context window of the model even "
                "after compaction"
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

    def brief(self) -> str:
        """Причина без текста нижнего слоя: для человека."""
        return self.reason.describe("")


@dataclass(frozen=True)
class SummaryAborted:
    reason: StopReason


SummaryOutcome = Summarized | NotSummarized | SummaryAborted


class SummaryInstruction:
    """Инструкция пересказа (docs/prompt_assembly/specs/04_compaction.md,
    2.3): основа, указания пользователя к ручному сжатию блоком
    «Additional Instructions:» и напоминание в конце. Создают цикл хода,
    пересказ и проверка профиля."""

    def __init__(self, templates: AgentTemplate) -> None:
        self._templates = templates

    def text(self, instructions: str) -> str:
        parts = [self._templates.read(AgentTemplateFile.SUMMARIZE_INSTRUCTION)]
        if instructions:
            custom = self._templates.read(AgentTemplateFile.SUMMARIZE_CUSTOM)
            parts.append(custom.format(instructions=instructions))

        parts.append(self._templates.read(AgentTemplateFile.SUMMARIZE_REMINDER))

        return "".join(parts)


@dataclass(frozen=True)
class SummaryPressure:
    """Чем вызвано сжатие. trigger — порог или переполнение (auto) либо
    команда пользователя (manual); last_resort — заполненность у предела,
    сервер уже отверг запрос или сжатие просил пользователь: если
    пересказывать по раундам нечего, разрешён последний рубеж; excess — на
    сколько токенов основной запрос превысил окно по словам сервера, None —
    сервер чисел не назвал или запроса не было; instructions — указания
    пользователя к пересказу, пусто — их нет."""

    trigger: CompactTrigger
    last_resort: bool
    excess: int | None
    instructions: str


@dataclass(frozen=True)
class SummaryJob:
    """Один пересказ: сессия, разговор после границы, снимок промпта,
    остановка хода и повод сжатия."""

    session: Session
    conversation: Sequence[ChainRecord]
    snapshot: PromptSnapshot
    cancellation: RunCancellation
    pressure: SummaryPressure


class Summarizer:
    """Запрос пересказа и разбор ответа — лестница
    docs/prompt_assembly/specs/04_compaction.md, 2.2.

    Пересказывается всё, кроме последней группы; запрос несёт тот же
    системный промпт и инструменты, что основной, плюс инструкцию сообщением
    пользователя (сборщик сливает её с последним текстом пользователя,
    когда отрезок кончается им). Если основной запрос уже превысил окно на
    известное число токенов, хвост сразу берёт столько последних групп,
    сколько покрывает превышение. Запрос пересказа, не влезший в окно,
    переносит в хвост последние группы пересказываемого — столько, сколько
    покрывает названное сервером превышение, иначе одну; голова не
    отбрасывается. Когда в пересказываемом не осталось ответа модели,
    при давлении (предел или отказ сервера) действует последний рубеж:
    пересказывается всё до последнего вопроса человека после последнего
    ответа, и только здесь при переполнении отбрасываются старшие группы —
    не больше LADDER_STEPS раз; остаток открывается пометкой об усечении,
    пересказ — оговоркой, что начало разговора он не покрывает. Создаёт
    цикл хода.
    """

    LADDER_STEPS: ClassVar[int] = 3
    HEAD_SHARE: ClassVar[float] = 0.2
    """Доля групп, отбрасываемая последним рубежом, когда превышение неизвестно."""

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
        self._signs = ContextOverflowSigns()
        self._instruction = SummaryInstruction(templates)
        self._ids = ids
        self._clock = clock
        self._version = version

    def unready(self, job: SummaryJob) -> NotSummarized | None:
        """Пересказывать нечего — без запроса к модели; None — пересказ
        пойдёт. Цикл хода спрашивает до того, как объявить сжатие начатым."""
        groups = self._grouper.groups(job.conversation)
        if len(groups) < 2:  # noqa: PLR2004 — начальная группа и хотя бы один ответ
            return NotSummarized(
                SummaryFailure.TOO_FEW_GROUPS, f"{len(groups)} group(s) in total"
            )

        if self._grouper.has_reply(self._flat(groups[:-1])):
            return None

        if not job.pressure.last_resort:
            return self._nothing_left("")

        split = self._last_prompt(job.conversation)
        if split is None:
            return self._nothing_left("")

        if not self._grouper.has_reply(job.conversation[:split]):
            return self._nothing_left("")

        return None

    async def summarize(self, job: SummaryJob) -> SummaryOutcome:
        unready = self.unready(job)
        if unready is not None:
            return unready

        groups = self._grouper.groups(job.conversation)
        sizes = self._sizes(groups)
        preserve = self._preserved_at_start(sizes, job.pressure.excess)
        overflow = ""
        while True:
            cut = max(0, len(groups) - preserve)
            head = self._flat(groups[:cut])
            if not self._grouper.has_reply(head):
                return await self._last_resort(job, overflow)

            request = self._request(job, head, 0)
            outcome = await self._attempt(request, job.cancellation)
            if isinstance(outcome, str):
                kept = self._flat(groups[cut:])
                return Summarized(
                    text=outcome, summarized=head, kept=kept, head_truncated=False
                )

            if not isinstance(outcome, Overflowed):
                return outcome

            overflow = outcome.error
            step = self._covering(list(reversed(sizes[:cut])), overflow)
            preserve += step
            logger.info(
                "compaction: the summary request overflowed, %d more round(s) "
                "move into the kept tail (%d kept of %d)",
                step,
                preserve,
                len(groups),
            )

    async def _last_resort(self, job: SummaryJob, overflow: str) -> SummaryOutcome:
        """Последний рубеж: всё до последнего вопроса человека после
        последнего ответа; переполнение отбрасывает старшие группы."""
        split = self._last_prompt(job.conversation)
        if not job.pressure.last_resort:
            return self._nothing_left(overflow)

        if split is None:
            return self._nothing_left(overflow)

        groups = self._grouper.groups(job.conversation[:split])
        kept = list(job.conversation[split:])
        dropped = 0
        for _attempt in range(self.LADDER_STEPS + 1):
            head = self._flat(groups[dropped:])
            if not self._grouper.has_reply(head):
                return self._nothing_left(overflow)

            request = self._request(job, head, dropped)
            outcome = await self._attempt(request, job.cancellation)
            if isinstance(outcome, str):
                return Summarized(
                    text=outcome, summarized=head, kept=kept, head_truncated=dropped > 0
                )

            if not isinstance(outcome, Overflowed):
                return outcome

            overflow = outcome.error
            dropped += self._dropped(self._sizes(groups[dropped:]), overflow)
            logger.info(
                "compaction: the last-resort summary request overflowed, "
                "%d oldest round(s) dropped",
                dropped,
            )

        return NotSummarized(SummaryFailure.REQUEST_TOO_LONG, overflow)

    def _preserved_at_start(self, sizes: Sequence[int], excess: int | None) -> int:
        """Сколько последних групп хвост берёт сразу: одну; при известном
        превышении основного запроса и больше чем трёх группах — ещё столько,
        сколько покрывает превышение сверх последней группы."""
        if excess is None:
            return 1

        if len(sizes) <= 3:  # noqa: PLR2004 — правило разбора: больше трёх групп
            return 1

        need = excess - sizes[-1]
        before = list(reversed(sizes[:-1]))

        return 1 + self._count_covering(before, need)

    def _covering(self, newest_first: Sequence[int], overflow: str) -> int:
        """Сколько групп с конца перенести в хвост: покрывающих превышение,
        названное сервером, иначе одну."""
        excess = self._signs.excess(overflow)
        if excess is None:
            return 1

        return max(1, self._count_covering(newest_first, excess))

    def _dropped(self, oldest_first: Sequence[int], overflow: str) -> int:
        """Сколько старших групп отбросить: покрывающих превышение, иначе
        HEAD_SHARE групп, не меньше одной."""
        excess = self._signs.excess(overflow)
        if excess is None:
            return max(1, int(len(oldest_first) * self.HEAD_SHARE))

        return max(1, self._count_covering(oldest_first, excess))

    @staticmethod
    def _count_covering(sizes: Sequence[int], need: int) -> int:
        """Сколько первых размеров набирают need; need не больше нуля — ноль."""
        if need <= 0:
            return 0

        total = 0
        for count, size in enumerate(sizes, start=1):
            total += size
            if total >= need:
                return count

        return len(sizes)

    def _sizes(self, groups: Sequence[Sequence[ChainRecord]]) -> list[int]:
        sizes: list[int] = []
        for group in groups:
            sizes.append(self._estimator.records(group))

        return sizes

    @staticmethod
    def _last_prompt(conversation: Sequence[ChainRecord]) -> int | None:
        """Позиция последнего вопроса человека после последнего ответа
        модели; None — после ответа вопросов нет."""
        for index in range(len(conversation) - 1, -1, -1):
            record = conversation[index]
            if isinstance(record, AssistantRecord) and not record.is_synthetic():
                return None

            if isinstance(record, UserRecord) and record.human_text() is not None:
                return index

        return None

    @staticmethod
    def _nothing_left(overflow: str) -> NotSummarized:
        """В пересказываемом не осталось ответа модели: без переполнения —
        пересказывать нечего, после переполнений — запрос не влез."""
        if not overflow:
            return NotSummarized(
                SummaryFailure.TOO_FEW_GROUPS,
                "the part to summarize holds no model reply",
            )

        return NotSummarized(
            SummaryFailure.REQUEST_TOO_LONG,
            f"{overflow} (no part with a model reply is left to summarize)",
        )

    def _request(
        self, job: SummaryJob, head: Sequence[ChainRecord], dropped: int
    ) -> ChatRequest:
        """Запрос пересказа отрезка; после отброшенных групп отрезок
        начинается ответом модели, и перед ним встаёт пометка об усечении."""
        sent: list[ChainRecord] = []
        if dropped:
            marker = self._templates.read(AgentTemplateFile.TRUNCATED_FOR_RETRY)
            sent.append(self._user(job.session, marker))

        sent.extend(head)
        instruction = self._instruction.text(job.pressure.instructions)
        sent.append(self._user(job.session, instruction))

        return self._builder.summary(sent, job.conversation, job.snapshot)

    async def _attempt(
        self, request: ChatRequest, cancellation: RunCancellation
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

        return self._parsed(asked.reply)

    def _parsed(self, reply: ChatReply) -> str | NotSummarized:
        if reply.tool_calls:
            names = [call.name for call in reply.tool_calls]
            return NotSummarized(SummaryFailure.TOOL_CALLS, ", ".join(names))

        if not reply.content.strip():
            return NotSummarized(SummaryFailure.EMPTY, "")

        # разбор <analysis> вырезается первым: модель упоминает в нём тег
        # <summary> словами, и первым блоком оказался бы кусок разбора
        reply_text = ReplyTag.ANALYSIS.without(reply.content)
        text = ReplyTag.SUMMARY.extract(reply_text)
        detail = "the <summary> block is empty"
        if text is None:
            # без блока пересказ — весь ответ без разбора, как у эталона
            text = reply_text
            detail = "the reply holds only the <analysis> block"

        text = text.strip()
        if not text:
            return NotSummarized(SummaryFailure.EMPTY, detail)

        return text

    def _user(self, session: Session, text: str) -> MetaRecord:
        """Текст запроса пересказа — инструкция или пометка об усечении — как
        служебная запись пользователя для сборщика; в сессию не попадает."""
        return MetaRecord(
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

            if isinstance(record, ToolResultRecord):
                yield from self._results(record, called)

    @staticmethod
    def _carried(record: AttachmentRecord) -> Iterator[tuple[str, str]]:
        attachment = record.attachment
        if not isinstance(attachment, InvokedSkillsAttachment):
            return

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
        record: ToolResultRecord, called: dict[str, str]
    ) -> Iterator[tuple[str, str]]:
        for result in record.message.tool_results():
            name = called.get(result.tool_use_id)
            if name is None:
                continue

            if result.is_error:
                continue

            yield name, result.text()


@dataclass(frozen=True)
class CompactJob:
    """Данные пачки, которые знает цикл хода: идентификатор сжатия (он же
    uuid пересказа и compactionId наружу), заполненность до сжатия,
    длительность и сегодняшняя дата."""

    compaction_id: str
    trigger: CompactTrigger
    pre_tokens: int
    duration_ms: int
    today: str


@dataclass(frozen=True)
class CompactPack:
    """Пачка сжатия до записи в сессию: записи по порядку, оценка того, что
    после границы уходит модели, и запись пересказа."""

    records: Sequence[Record]
    post_tokens: int
    summary: CompactSummaryRecord


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

    def pack(
        self, session: Session, summary: Summarized, job: CompactJob
    ) -> CompactPack:
        """Записи пачки без seq — в сессию их кладёт `add`; логический
        родитель границы — последняя запись разговора, то есть последняя
        запись хвоста. uuid пересказа — идентификатор сжатия job."""
        discovered = Announced(session.conversation()).discovered()
        boundary_uuid = self._ids.uuid()
        date = self._attachments.date(boundary_uuid, job.today, False)
        summary_record = self._summary_record(
            session, date.uuid, summary, job.compaction_id
        )
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
                trigger=job.trigger,
                pre_tokens=job.pre_tokens,
                post_tokens=post_tokens,
                duration_ms=job.duration_ms,
                preserved_messages=PreservedMessages(
                    anchor_uuid=summary_record.uuid,
                    uuids=kept_uuids,
                    all_uuids=kept_uuids,
                ),
                pre_compact_discovered_tools=discovered,
            ),
        )

        records: list[Record] = [boundary, date, summary_record, *inputs]

        return CompactPack(
            records=records,
            post_tokens=post_tokens,
            summary=summary_record,
        )

    @staticmethod
    def add(session: Session, pack: CompactPack) -> Sequence[Record]:
        """Записи пачки в сессию разом, без точек ожидания: писатель положит
        их одним `append`."""
        added: list[Record] = []
        for record in pack.records:
            added.append(session.add(record))

        return added

    def _summary_record(
        self, session: Session, parent: str, summary: Summarized, uuid: str
    ) -> CompactSummaryRecord:
        """Пересказ в обёртке; без начала разговора — с оговоркой об этом."""
        text = summary.text
        if summary.head_truncated:
            text = self._templates.read(
                AgentTemplateFile.SUMMARY_HEAD_TRUNCATED
            ).format(summary=text)

        wrapped = self._templates.read(AgentTemplateFile.SUMMARY_WRAPPER).format(
            summary=text
        )

        return CompactSummaryRecord(
            uuid=uuid,
            parent_uuid=parent,
            timestamp=self._clock.now(),
            session_id=session.id.value,
            version=self._version,
            message=UserMessage(content=wrapped),
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
