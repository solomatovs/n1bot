"""Сжатие и подрезка контекста Claude Code 2.1.289.

По списку записей в памяти процесса предсказывает: заполненность контекста
перед запросом, уровень и решение автосжатия, разбиение на раунды и состав
сохранённого хвоста, тело запроса суммаризации, поля compactMetadata, порядок
записей после границы, вынос результата инструмента в файл и правку
context_management после простоя.

Ошибки:
CompactionError — запись не разбирается, текст инструкции суммаризации не
    читается либо нарушено предусловие операции; текст называет журнал,
    строку и причину. Ошибки загрузчика (LoaderError) и сборки запроса
    (RequestError, AssembleError) приходят упакованными в неё.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from boba.prompt_assembly.entries import AssembleError, Entry, MarkPath, UserEntry
from boba.prompt_assembly.loader import LoaderError, SessionMemory
from boba.prompt_assembly.records import BlockType, Encoding, Json, RecordType, Row, Synthetic
from boba.prompt_assembly.request import (
    Composition,
    ContextEdits,
    LaunchParams,
    RequestBuilder,
    RequestError,
)

Fields = Mapping[str, Json]



class CompactionError(Exception):
    """Единственная ошибка, которую модуль выпускает наружу."""


class CompactMark(StrEnum):
    COMPACT_BOUNDARY = "compact_boundary"
    CLEARED = "[Old tool result content cleared]"
    PERSISTED_OPEN = "<persisted-output>"
    PERSISTED_CLOSE = "</persisted-output>"
    HEAD_TRUNCATED = "[earlier conversation truncated for compaction retry]"
    PROMPT_TOO_LONG = "Prompt is too long"
    READ_TOOL = "Read"
    SKILL_LISTING = "skill_listing"
    ANALYSIS = r"<analysis>[\s\S]*?</analysis>"
    SUMMARY = r"<summary>([\s\S]*?)</summary>"
    BLANK_RUN = r"\n\n+"
    COMMAND_ARGS = r"<command-args>(.*?)</command-args>"
    INSTRUCTION_UUID = "summary-request"


class Limit(IntEnum):
    """Числовые константы бандла; имя в комментарии — минифицированное."""

    SUMMARY_BUFFER = 13_000  # zvt
    OUTPUT_RESERVE = 20_000  # tTt
    WARN_MARGIN = 20_000  # oMe: S-20000
    BLOCKING_MARGIN = 3_000  # Kvt
    WINDOW_MIN = 100_000  # IGe
    WINDOW_MAX = 1_000_000  # Ctt
    MAX_FAILURES = 3  # Kjt
    RAPID_TURNS = 3  # sTt
    RAPID_REFILLS = 3  # M2n
    HEAD_TRUNCATIONS = 3  # NY
    MEDIA_TOKENS = 2_000  # lzt
    REPORT_DIVISOR = 4  # Gc по умолчанию
    RESTORED_FILES = 5  # PNe
    RESTORED_FILE_TOKENS = 5_000  # MNe
    RESTORED_FILES_BUDGET = 50_000  # D$o
    SKILL_TOKENS = 5_000  # F$o
    SKILLS_BUDGET = 25_000  # INe
    SUBSTANTIVE_USER_TOKENS = 1_000  # a$o
    PERSIST_CEILING = 50_000  # mL
    PREVIEW_CHARS = 2_000  # wxe
    BASH_INLINE = 30_000  # t9n
    MESSAGE_BUDGET = 200_000  # zTn
    IDLE_SECONDS = 3_900  # Mn.minIdleSeconds
    IDLE_KEEP = 5  # Mn.keepToolUses
    IDLE_CLEAR_AT_LEAST = 20_000  # Mn.clearAtLeastTokens
    IDLE_BELOW = 3  # Mn.triggerToolUsesBelowCount
    IDLE_MIN_TRIGGER = 20  # Mn.minTriggerToolUses
    IDLE_MIN_CHARS = 64  # dr


class Trigger(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class Level(StrEnum):
    OK = "ok"
    WARN = "warn"
    COMPACT = "compact"
    BLOCKED = "blocked"


class WindowSource(StrEnum):
    ENV = "env"
    SETTINGS = "settings"
    MODEL_DEFAULT = "model-default"
    AUTO = "auto"


class DecisionKind(StrEnum):
    NOT_NEEDED = "not_needed"
    FAILURE_BREAKER_OPEN = "failure_breaker_open"
    RAPID_REFILL_TRIPPED = "rapid_refill_breaker_tripped"
    REACTIVE = "reactive"
    CLASSIC = "classic"


class SplitKind(StrEnum):
    ROUND = "round"
    SUMMARIZE_ALL = "summarize_all"


class LadderEnd(StrEnum):
    TOO_FEW_GROUPS = "too_few_groups"
    EXHAUSTED = "exhausted"


class QuerySource(StrEnum):
    COMPACT = "compact"
    PROMPT_SUGGESTION = "prompt_suggestion"
    AWAY_SUMMARY = "away_summary"
    AGENT_SUMMARY = "agent_summary"
    HOOK_PROMPT = "hook_prompt"


class JournalOnly(StrEnum):
    """Вложения, которые пишутся только в журнал и в список сообщений клиента не входят."""

    PROMPT_SNAPSHOT = "prompt_snapshot"
    DEFERRED_TOOLS_RECORD = "deferred_tools_record"


class RequestStage(StrEnum):
    """Вложения, которые создаёт стадия сборки запроса — после проверки порога."""

    SESSION_CONTEXT = "session_context"
    DATE = "date"
    CREDENTIAL_ORG = "credential_org"
    REMOTE_SESSION_CHANGE = "remote_session_change"


class Header(StrEnum):
    COMPACTION_REQUEST = "x-cc-compaction-request"
    COMPACTION = "x-claude-code-compaction"
    REQUEST_CLASS = "x-claude-code-request-class"
    CLASS_COMPACTION = "compaction"
    BETA_FALLBACK = "server-side-fallback-2026-07-01"
    BETA_FALLBACK_CREDIT = "fallback-credit-2026-06-01"
    BETA_CACHE_TTL = "extended-cache-ttl-2025-04-11"


class Clearable(StrEnum):
    """Инструменты, чьи результаты подлежат очистке (Pt @203566400)."""

    READ = "Read"
    BASH = "Bash"
    POWERSHELL = "PowerShell"
    GREP = "Grep"
    GLOB = "Glob"
    WEB_SEARCH = "WebSearch"
    WEB_FETCH = "WebFetch"
    EDIT = "Edit"
    WRITE = "Write"


class AlwaysExcluded(StrEnum):
    """Инструменты, всегда исключённые из серверной очистки ($l)."""

    GET_TASK = "GetTask"
    POLL = "Poll"


class AttachmentRank(IntEnum):
    """Порядок вложений после сводки (Cle @209424900): меньший ранг раньше."""

    FILE = 10
    TASK_STATUS = 20
    QUEUED_COMMAND = 30
    PLAN_FILE = 40
    PLAN_MODE = 50
    INVOKED_SKILLS = 60
    DELTA = 70
    HOOK = 90


@dataclass(frozen=True)
class Usage:
    """Счётчики usage ответа API; total — сумма Px."""

    input_tokens: int
    cache_creation: int
    cache_read: int
    output_tokens: int

    @property
    def prompt(self) -> int:
        return self.input_tokens + self.cache_creation + self.cache_read

    @property
    def total(self) -> int:
        return self.prompt + self.output_tokens


@dataclass(frozen=True)
class Message(Row):
    """Запись списка сообщений клиента: строка журнала плюс состояние usage.

    Создаётся CompactionLog'ом из строк SessionMemory. usage_zeroed выставлен
    у сообщений сохранённого хвоста: клиент обнуляет им usage (b_e), чтобы
    якорь подсчёта не взял старое число.
    """

    usage_zeroed: bool = False

    @property
    def metadata(self) -> Fields:
        return self._fields(self.raw.get("compactMetadata"))

    @property
    def reply_id(self) -> str:
        """message.id настоящего ответа API; пусто у прочих записей и у синтетических."""
        if self.kind != RecordType.ASSISTANT:
            return ""

        if self.model == Synthetic.MODEL:
            return ""

        return self._text(self.message, "id")

    @property
    def model(self) -> str:
        return self._text(self.message, "model")

    @property
    def rendered(self) -> Sequence[Json]:
        """Содержимое сообщений, которыми вложение уходит модели (поле rendered)."""
        rendered = self.raw.get("rendered")
        if not isinstance(rendered, list):
            return ()

        result: list[Json] = []
        for item in rendered:
            if isinstance(item, dict):
                result.append(item.get("content"))

        return result

    @property
    def is_boundary(self) -> bool:
        if self.kind != RecordType.SYSTEM:
            return False

        return self.raw.get("subtype") == CompactMark.COMPACT_BOUNDARY

    @property
    def is_summary(self) -> bool:
        return self.raw.get("isCompactSummary") is True

    @property
    def is_meta(self) -> bool:
        return self.raw.get("isMeta") is True

    @property
    def is_api_error(self) -> bool:
        return self.raw.get("isApiErrorMessage") is True

    @property
    def is_request_stage(self) -> bool:
        if self.kind != RecordType.ATTACHMENT:
            return False

        return self.attachment_kind in frozenset(RequestStage)

    @property
    def usage(self) -> Usage | None:
        """usage ответа по правилам _j/zpe: синтетика, isUnmetered и обнулённый хвост usage не дают."""
        if self.kind != RecordType.ASSISTANT:
            return None

        if self.usage_zeroed:
            return None

        if self.model == Synthetic.MODEL:
            return None

        if self.raw.get("isUnmetered") is True:
            return None

        raw = self.message.get("usage")
        if not isinstance(raw, dict):
            return None

        usage = self._usage(self._last_iteration(raw))
        if usage.prompt == 0:
            return None

        return usage

    @property
    def has_tool_result(self) -> bool:
        for block in self.blocks:
            if block.get("type") == BlockType.TOOL_RESULT:
                return True

        return False

    def zeroed(self) -> "Message":
        return replace(self, usage_zeroed=True)

    def _last_iteration(self, usage: Fields) -> Fields:
        iterations = usage.get("iterations")
        if not isinstance(iterations, list):
            return usage

        for item in reversed(iterations):
            if not isinstance(item, dict):
                continue
            if item.get("type") in ("message", "fallback_message"):
                return item

        return usage

    def _usage(self, raw: Fields) -> Usage:
        return Usage(
            input_tokens=self._int(raw, "input_tokens"),
            cache_creation=self._int(raw, "cache_creation_input_tokens"),
            cache_read=self._int(raw, "cache_read_input_tokens"),
            output_tokens=self._int(raw, "output_tokens"),
        )

    def _int(self, holder: Fields, key: str) -> int:
        value = holder.get(key)
        if isinstance(value, bool):
            return 0

        if isinstance(value, int):
            return value

        return 0

    def _text(self, holder: Fields, key: str) -> str:
        value = holder.get(key)
        if isinstance(value, str):
            return value

        return ""

    def _fields(self, value: Json) -> Fields:
        if isinstance(value, dict):
            return value

        return {}


class CompactionLog:
    """Список сообщений клиента в терминах модели сжатия.

    Обёртка над SessionMemory: превращает строки в Message, отбрасывает
    записи побочных цепочек и вложения, которые живут только в журнале, и
    обнуляет usage сохранённому хвосту (w$o/S_e @209383000). Создаётся
    сверкой и командой compaction.
    """

    def __init__(self, memory: SessionMemory) -> None:
        self._memory = memory
        self._journal_only = frozenset(JournalOnly)
        self.messages: list[Message] = []
        seen: set[str] = set()

        for row in memory.rows:
            if not self._kept(row) or row.uuid in seen:
                continue
            seen.add(row.uuid)
            self.messages.append(Message(line=row.line, raw=row.raw))

    def process_start(self, line: int) -> int:
        """Первая строка процесса, записавшего строку line."""
        return max(self._memory.cut_for(line), 1)

    def boundaries(self) -> list[Message]:
        result: list[Message] = []
        for message in self.messages:
            if message.is_boundary:
                result.append(message)

        return result

    def window_before(self, line: int) -> list[Message]:
        """Список в памяти перед строкой line: от последней границы, хвост — после якоря."""
        try:
            window = self._memory.window_before(line)
        except LoaderError as exc:
            raise CompactionError(
                f"restoring the message list of {self._memory.journal.path} before line {line} failed: {exc}"
            ) from exc

        result: list[Message] = []
        for row in window.rows:
            if not self._kept(row):
                continue
            result.append(Message(line=row.line, raw=row.raw, usage_zeroed=row.uuid in window.preserved))

        return result

    def _kept(self, row: Row) -> bool:
        if row.sidechain:
            return False

        return row.attachment_kind not in self._journal_only


class TokenEstimator:
    """Грубая оценка токенов по длине текста (qm @208282313, Z4/lzt/Gc @203551178).

    Делитель — символов на токен: 3 для новых моделей в проверке порога
    (kg @202449422), 4 — когда вызывающий делитель не передал (preTokens,
    postTokens, размеры групп лестницы). Длина считается в единицах UTF-16,
    округление — Math.round.
    """

    def __init__(self, chars_per_token: int) -> None:
        if chars_per_token <= 0:
            raise CompactionError(f"token estimator expects a positive divisor, got {chars_per_token}")

        self._divisor = chars_per_token

    def text(self, value: str) -> int:
        units = len(value.encode(Encoding.UTF16_LE, Encoding.SURROGATES)) // 2
        return (2 * units + self._divisor) // (2 * self._divisor)

    def content(self, content: Json) -> int:
        if isinstance(content, str):
            return self.text(content)

        if not isinstance(content, list):
            return 0

        total = 0
        for item in content:
            total += self.block(item)

        return total

    def block(self, block: Json) -> int:
        if isinstance(block, str):
            return self.text(block)

        if not isinstance(block, dict):
            return 0

        kind = block.get("type")

        if kind == BlockType.TEXT:
            return self._field(block, "text")

        if kind in (BlockType.IMAGE, BlockType.DOCUMENT):
            return int(Limit.MEDIA_TOKENS)

        if kind == BlockType.TOOL_RESULT:
            return self.content(block.get("content"))

        if kind == BlockType.TOOL_USE:
            name = block.get("name")
            if not isinstance(name, str):
                name = ""
            tool_input = block.get("input")
            if tool_input is None:
                tool_input = {}
            return self.text(name + self.stringify(tool_input))

        if kind == BlockType.THINKING:
            return self._field(block, "thinking")

        if kind == BlockType.REDACTED_THINKING:
            return self._field(block, "data")

        return self.text(self.stringify(block))

    def message(self, message: Message) -> int:
        if message.kind in (RecordType.USER, RecordType.ASSISTANT):
            return self.content(message.content)

        if message.kind != RecordType.ATTACHMENT:
            return 0

        total = 0
        for content in message.rendered:
            total += self.content(content)

        return total

    def messages(self, messages: Sequence[Message]) -> int:
        total = 0
        for message in messages:
            total += self.message(message)

        return total

    def stringify(self, value: Json) -> str:
        """JSON.stringify: без пробелов, не-ASCII без экранирования."""
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def _field(self, block: Fields, key: str) -> int:
        value = block.get(key)
        if isinstance(value, str):
            return self.text(value)

        return 0


@dataclass(frozen=True)
class UsageAnchor:
    """Якорь подсчёта: число токенов из usage и индекс, после которого идёт оценка."""

    tokens: int
    index: int


@dataclass(frozen=True)
class Fill:
    """Заполненность контекста: якорь, оценка хвоста и поправка серверной очистки."""

    anchor_tokens: int
    tail_tokens: int
    server_cleared: int

    @property
    def total(self) -> int:
        return self.anchor_tokens + self.tail_tokens + self.server_cleared


class ContextMeter:
    """Подсчёт заполненности контекста по списку сообщений (Am @208549146, vvt, UO).

    Якорь — последний ответ с ненулевым usage (сдвинутый к первой записи того
    же message.id) либо граница сжатия (0 токенов); всё после якоря
    оценивается TokenEstimator'ом. Вложения стадии сборки запроса, стоящие
    после якоря-ответа, на момент проверки ещё не созданы и не считаются.
    """

    def __init__(self, estimator: TokenEstimator) -> None:
        self._estimator = estimator

    def anchor(self, messages: Sequence[Message]) -> UsageAnchor | None:
        index = len(messages) - 1

        while index >= 0:
            message = messages[index]

            if message.is_boundary:
                return UsageAnchor(tokens=0, index=index)

            usage = message.usage
            if usage is not None:
                return UsageAnchor(tokens=usage.total, index=self._first_fragment(messages, index))

            index -= 1

        return None

    def fill(self, messages: Sequence[Message]) -> Fill:
        anchor = self.anchor(messages)
        if anchor is None:
            return Fill(anchor_tokens=0, tail_tokens=self._estimator.messages(self._counted(messages, False)), server_cleared=0)

        tail = messages[anchor.index + 1 :]
        after_boundary = messages[anchor.index].is_boundary
        return Fill(
            anchor_tokens=anchor.tokens,
            tail_tokens=self._estimator.messages(self._counted(tail, after_boundary)),
            server_cleared=self.server_cleared(messages),
        )

    def tokens(self, messages: Sequence[Message]) -> int:
        """Am без поправки серверной очистки — так считаются preTokens."""
        fill = self.fill(messages)
        return fill.anchor_tokens + fill.tail_tokens

    def server_cleared(self, messages: Sequence[Message]) -> int:
        """UO: cleared_input_tokens серверной очистки из последнего учтённого ответа."""
        for message in reversed(messages):
            if message.is_boundary:
                return 0

            if message.usage is None:
                continue

            management = message.message.get("context_management")
            if not isinstance(management, dict):
                if message.message.get("stop_reason") is None:
                    continue
                return 0

            return self._cleared(management)

        return 0

    def _counted(self, tail: Sequence[Message], after_boundary: bool) -> list[Message]:
        if after_boundary:
            return list(tail)

        result: list[Message] = []
        for message in tail:
            if message.is_request_stage:
                continue
            result.append(message)

        return result

    def _first_fragment(self, messages: Sequence[Message], index: int) -> int:
        message_id = messages[index].reply_id
        if not message_id:
            return index

        first = index
        probe = index - 1

        while probe >= 0:
            other = messages[probe].reply_id
            if other == message_id:
                first = probe
            elif other:
                break
            probe -= 1

        return first

    def _cleared(self, management: Fields) -> int:
        edits = management.get("applied_edits")
        if not isinstance(edits, list):
            return 0

        total = 0
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            if edit.get("type") != IdleEdit.TYPE:
                continue
            cleared = edit.get("cleared_input_tokens")
            if isinstance(cleared, int):
                total += cleared

        return total


class IdleEdit(StrEnum):
    TYPE = "clear_tool_uses_20250919"
    TOOL_USES = "tool_uses"
    INPUT_TOKENS = "input_tokens"
    THINKING = "clear_thinking_20251015"


@dataclass(frozen=True)
class ThresholdPolicy:
    """Порог автосжатия и уровни заполненности (Nw @208557000, OK, dK @208555400, oMe).

    Собирается из фактов о модели и переменных окружения процесса; им
    пользуется AutoCompactGate. window_override — CLAUDE_CODE_AUTO_COMPACT_WINDOW,
    pct_override — CLAUDE_AUTOCOMPACT_PCT_OVERRIDE, blocking_override —
    CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE.
    """

    context_window: int
    max_output_tokens: int
    native_1m: bool = True
    enabled: bool = True
    window_override: int | None = None
    settings_window: int | None = None
    pct_override: float | None = None
    blocking_override: int | None = None

    def __post_init__(self) -> None:
        if self.context_window <= 0:
            raise CompactionError(f"threshold policy expects a positive context window, got {self.context_window}")

    @property
    def source(self) -> WindowSource:
        if self._env_window() is not None:
            return WindowSource.ENV

        if self.settings_window is not None:
            return WindowSource.SETTINGS

        if self.context_window >= Limit.WINDOW_MAX and self.enabled and self.native_1m:
            return WindowSource.MODEL_DEFAULT

        return WindowSource.AUTO

    @property
    def window(self) -> int:
        if not self.enabled:
            return self.context_window

        env = self._env_window()
        if env is not None:
            return min(self.context_window, env)

        if self.settings_window is not None:
            return min(self.context_window, self.settings_window)

        return self.context_window

    @property
    def effective_window(self) -> int:
        return self.window - min(self.max_output_tokens, int(Limit.OUTPUT_RESERVE))

    @property
    def threshold(self) -> int:
        base = self.effective_window - int(Limit.SUMMARY_BUFFER)
        pct = self.pct_override

        if pct is None:
            return base

        if math.isnan(pct):
            return base

        if pct <= 0:
            return base

        if pct > 100:
            return base

        return min(math.floor(self.effective_window * (pct / 100)), base)

    @property
    def blocking_limit(self) -> int:
        override = self.blocking_override
        if override is not None and override > 0:
            return override

        raw = self.context_window - min(self.max_output_tokens, int(Limit.OUTPUT_RESERVE))
        return raw - int(Limit.BLOCKING_MARGIN)

    @property
    def warn_from(self) -> int:
        if self.enabled:
            return self.threshold - int(Limit.WARN_MARGIN)

        return self.effective_window - int(Limit.WARN_MARGIN)

    def level(self, tokens: int) -> Level:
        if tokens >= self.blocking_limit:
            return Level.BLOCKED

        if self.enabled and tokens >= self.threshold:
            return Level.COMPACT

        if tokens >= self.warn_from:
            return Level.WARN

        return Level.OK

    def _env_window(self) -> int | None:
        if self.window_override is None:
            return None

        if self.window_override <= 0:
            return None

        clamped = min(self.window_override, int(Limit.WINDOW_MAX))
        return max(int(Limit.WINDOW_MIN), clamped)


@dataclass(frozen=True)
class CompactTracking:
    """Состояние защиты от зацикливания в пределах одного хода (compactTracking цикла запроса).

    Заводится пустым в начале каждого хода (@215800706: compactTracking:void 0),
    поэтому счётчики неудач и перезаполнений между ходами не переносятся.
    """

    compacted: bool = False
    turn_counter: int = 0
    consecutive_failures: int = 0
    consecutive_rapid_refills: int = 0

    def rapid_refills(self) -> int:
        """seo: сжатие, случившееся менее чем через три итерации после прошлого, — перезаполнение."""
        if not self.compacted:
            return 0

        if self.turn_counter >= Limit.RAPID_TURNS:
            return 0

        return self.consecutive_rapid_refills + 1

    def after_success(self, rapid_refills: int) -> "CompactTracking":
        return CompactTracking(compacted=True, turn_counter=0, consecutive_failures=0, consecutive_rapid_refills=rapid_refills)

    def after_failure(self) -> "CompactTracking":
        return replace(self, consecutive_failures=self.consecutive_failures + 1)

    def after_tool_round(self) -> "CompactTracking":
        if not self.compacted:
            return self

        return replace(self, turn_counter=self.turn_counter + 1)


@dataclass(frozen=True)
class Decision:
    """Решение перед запросом: что делает автосжатие и чем кончится ход, если оно не поможет."""

    kind: DecisionKind
    level: Level
    tokens: int
    rapid_refills: int = 0
    allow_last_resort: bool = False

    @property
    def compacts(self) -> bool:
        return self.kind in (DecisionKind.REACTIVE, DecisionKind.CLASSIC)


class AutoCompactGate:
    """Решение «сжимать ли перед этим запросом» (g0r @209433905, hBo @209432800).

    Получает порог (ThresholdPolicy) и счётчик заполненности (ContextMeter с
    делителем модели); вызывается циклом запроса перед каждым обращением к API.
    remote — признак CLAUDE_CODE_REMOTE без флага реактивного пути.
    """

    def __init__(self, policy: ThresholdPolicy, meter: ContextMeter, disabled_by_env: bool = False, reactive: bool = True) -> None:
        self._policy = policy
        self._meter = meter
        self._disabled = disabled_by_env
        self._reactive = reactive
        self._silent_sources = frozenset(
            (QuerySource.COMPACT, QuerySource.PROMPT_SUGGESTION, QuerySource.AWAY_SUMMARY, QuerySource.AGENT_SUMMARY, QuerySource.HOOK_PROMPT)
        )

    def decide(self, messages: Sequence[Message], tracking: CompactTracking, query_source: str) -> Decision:
        tokens = self._meter.fill(messages).total
        level = self._policy.level(tokens)

        if self._disabled:
            return Decision(kind=DecisionKind.NOT_NEEDED, level=level, tokens=tokens)

        if tracking.consecutive_failures >= Limit.MAX_FAILURES:
            return Decision(kind=DecisionKind.FAILURE_BREAKER_OPEN, level=level, tokens=tokens)

        if not self._should_compact(level, query_source):
            return Decision(kind=DecisionKind.NOT_NEEDED, level=level, tokens=tokens)

        rapid = tracking.rapid_refills()
        if rapid >= Limit.RAPID_REFILLS:
            return Decision(kind=DecisionKind.RAPID_REFILL_TRIPPED, level=level, tokens=tokens, rapid_refills=rapid)

        kind = DecisionKind.CLASSIC
        if self._reactive and self._policy.source is not WindowSource.AUTO:
            kind = DecisionKind.REACTIVE

        return Decision(kind=kind, level=level, tokens=tokens, rapid_refills=rapid, allow_last_resort=level is Level.BLOCKED)

    def blocks(self, messages: Sequence[Message], compacted: bool, query_source: str) -> bool:
        """Блокирующий предел (@215819447): ход кончается ошибкой «Prompt is too long»."""
        if compacted:
            return False

        if query_source == QuerySource.COMPACT:
            return False

        if self._policy.enabled and self._reactive and self._policy.source is WindowSource.AUTO:
            return False

        return self._policy.level(self._meter.tokens(messages)) is Level.BLOCKED

    def _should_compact(self, level: Level, query_source: str) -> bool:
        if query_source in self._silent_sources:
            return False

        if not self._policy.enabled:
            return False

        if self._reactive and self._policy.source is WindowSource.AUTO:
            return False

        return level in (Level.COMPACT, Level.BLOCKED)


class RoundGrouper:
    """Разбиение истории на раунды API (KU @209337674).

    Новая группа начинается на записи assistant с message.id, отличным от
    предыдущего ответа; всё до первого ответа — отдельная начальная группа.
    Разрез хвоста идёт только по границам групп, поэтому пары
    tool_use/tool_result не рвутся.
    """

    def groups(self, messages: Sequence[Message]) -> list[list[Message]]:
        result: list[list[Message]] = []
        current: list[Message] = []
        last_id = ""

        for message in messages:
            starts = self._starts_round(message, last_id)

            if starts and current:
                result.append(current)
                current = [message]
            else:
                current.append(message)

            if message.kind == RecordType.ASSISTANT:
                last_id = self._response_id(message)

        if current:
            result.append(current)

        return result

    def _starts_round(self, message: Message, last_id: str) -> bool:
        if message.kind != RecordType.ASSISTANT:
            return False

        if message.raw.get("resumedFromIncompleteThinking") is True:
            return False

        return self._response_id(message) != last_id

    def _response_id(self, message: Message) -> str:
        value = message.message.get("id")
        if isinstance(value, str):
            return value

        return ""


@dataclass(frozen=True)
class Attempt:
    """Одна попытка суммаризации: что уходит в запрос и что остаётся дословно."""

    to_summarize: tuple[Message, ...]
    to_keep: tuple[Message, ...]
    split: SplitKind
    groups_preserved: int
    total_groups: int
    head_truncations: int = 0

    @property
    def head_truncated(self) -> bool:
        return self.head_truncations > 0


@dataclass(frozen=True)
class LadderStop:
    """Лестница кончилась без сводки: сжатие считается неудачей."""

    reason: LadderEnd
    attempts: int
    total_groups: int


class SummarizeLadder:
    """Лестница реактивного сжатия (vle @209358700) над одним списком сообщений.

    Создаётся на одно сжатие. first() даёт первую попытку — всё, кроме
    последнего раунда; too_long(gap) — следующую после ответа «prompt too
    long»: хвост растёт на число раундов, покрывающее разрыв, а когда
    суммаризовать по раундам больше нечего и разрешён последний рубеж —
    режется по последнему пользовательскому промпту и затем отбрасывает
    голову. Ветка «первый раунд отдельно» (ZHt) не моделируется.
    """

    def __init__(self, messages: Sequence[Message], allow_last_resort: bool, initial_gap: int | None = None) -> None:
        self._grouper = RoundGrouper()
        self._sizer = TokenEstimator(int(Limit.REPORT_DIVISOR))
        self._groups = self._grouper.groups(self._without_progress(messages))
        self._allow_last_resort = allow_last_resort
        self._preserve = 1
        self._attempts = 0
        self._last_resort: Attempt | None = None
        self._seed(initial_gap)

    @property
    def total_groups(self) -> int:
        return len(self._groups)

    def first(self) -> Attempt | LadderStop:
        if len(self._groups) < 2:
            return LadderStop(reason=LadderEnd.TOO_FEW_GROUPS, attempts=0, total_groups=len(self._groups))

        return self._current(None)

    def too_long(self, gap: int | None) -> Attempt | LadderStop:
        """Следующая попытка после ответа «prompt too long» с разрывом gap токенов."""
        previous = self._last_resort

        if previous is not None:
            return self._truncate_head(previous, gap)

        summarized = len(self._groups) - self._preserve
        self._preserve += self._step(gap, summarized)
        return self._current(gap)

    def _current(self, gap: int | None) -> Attempt | LadderStop:
        total = len(self._groups)
        cut = max(0, total - self._preserve)
        to_summarize = self._flat(self._groups[:cut])

        if self._has_assistant(to_summarize):
            self._attempts += 1
            return Attempt(
                to_summarize=tuple(to_summarize),
                to_keep=tuple(self._flat(self._groups[cut:])),
                split=SplitKind.ROUND,
                groups_preserved=self._preserve,
                total_groups=total,
            )

        if not self._allow_last_resort:
            return LadderStop(reason=self._end(), attempts=self._attempts, total_groups=total)

        return self._enter_last_resort(gap)

    def _enter_last_resort(self, gap: int | None) -> Attempt | LadderStop:
        """g$o: суммаризуется всё до последнего пользовательского промпта после последнего ответа."""
        everything = self._flat(self._groups)
        split = self._last_prompt_split(everything)
        to_summarize = everything[:split]
        to_keep = everything[split:]
        truncations = 0

        if gap is not None:
            shortened = self._drop_head(to_summarize, gap)
            if shortened is None:
                return LadderStop(reason=self._end(), attempts=self._attempts, total_groups=len(self._groups))
            to_summarize = shortened
            truncations = 1

        if not self._substantive([*to_summarize, *to_keep]):
            return LadderStop(reason=self._end(), attempts=self._attempts, total_groups=len(self._groups))

        self._attempts += 1
        attempt = Attempt(
            to_summarize=tuple(to_summarize),
            to_keep=tuple(to_keep),
            split=SplitKind.SUMMARIZE_ALL,
            groups_preserved=0,
            total_groups=len(self._groups),
            head_truncations=truncations,
        )
        self._last_resort = attempt
        return attempt

    def _truncate_head(self, previous: Attempt, gap: int | None) -> Attempt | LadderStop:
        if previous.head_truncations >= Limit.HEAD_TRUNCATIONS:
            return LadderStop(reason=LadderEnd.EXHAUSTED, attempts=self._attempts, total_groups=len(self._groups))

        shortened = self._drop_head(list(previous.to_summarize), gap)
        if shortened is None:
            return LadderStop(reason=LadderEnd.EXHAUSTED, attempts=self._attempts, total_groups=len(self._groups))

        if not self._substantive([*shortened, *previous.to_keep]):
            return LadderStop(reason=LadderEnd.EXHAUSTED, attempts=self._attempts, total_groups=len(self._groups))

        self._attempts += 1
        attempt = replace(previous, to_summarize=tuple(shortened), head_truncations=previous.head_truncations + 1)
        self._last_resort = attempt
        return attempt

    def _drop_head(self, messages: Sequence[Message], gap: int | None) -> list[Message] | None:
        """wle @209403000: отбросить с головы раунды, покрывающие разрыв, либо 20 % раундов."""
        groups = self._grouper.groups(messages)
        if len(groups) < 2:
            return None

        dropped = max(1, math.floor(len(groups) * 0.2))
        if gap is not None:
            dropped = self._covering_from_head(groups, gap)

        dropped = min(dropped, len(groups) - 1)
        if dropped < 1:
            return None

        return self._flat(groups[dropped:])

    def _covering_from_head(self, groups: Sequence[Sequence[Message]], gap: int) -> int:
        covered = 0
        count = 0

        for group in groups:
            covered += self._sizer.messages(group)
            count += 1
            if covered >= gap:
                break

        return count

    def _seed(self, initial_gap: int | None) -> None:
        total = len(self._groups)

        if initial_gap is None:
            return

        if total <= 3:
            return

        remaining = initial_gap - self._sizer.messages(self._groups[total - 1])
        if remaining <= 0:
            return

        self._preserve = 1 + self._covering_from_tail(total - 1, remaining)

    def _step(self, gap: int | None, summarized: int) -> int:
        if gap is None:
            return 1

        return self._covering_from_tail(summarized, gap)

    def _covering_from_tail(self, upto: int, gap: int) -> int:
        """tjt: сколько раундов с конца покрывают разрыв; если почти все — половина."""
        covered = 0
        count = 0

        for index in range(upto - 1, -1, -1):
            covered += self._sizer.messages(self._groups[index])
            count += 1
            if covered >= gap:
                break

        if count >= upto - 1:
            return max(1, upto // 2)

        return count

    def _last_prompt_split(self, messages: Sequence[Message]) -> int:
        last_reply = -1
        for position, message in enumerate(messages):
            if self._real_assistant(message):
                last_reply = position
            if message.kind == RecordType.USER and message.has_tool_result:
                last_reply = position

        for position in range(len(messages) - 1, last_reply, -1):
            message = messages[position]
            if message.kind != RecordType.USER:
                continue
            if message.is_meta:
                continue
            if message.has_tool_result:
                continue
            return position

        return len(messages)

    def _substantive(self, messages: Sequence[Message]) -> bool:
        """ojt: есть настоящий ответ ассистента либо не меньше 1000 токенов пользовательского текста."""
        users: list[Message] = []

        for message in messages:
            if self._real_assistant(message):
                return True
            if message.kind == RecordType.USER and not message.is_meta:
                users.append(message)

        if not users:
            return False

        return self._sizer.messages(users) >= Limit.SUBSTANTIVE_USER_TOKENS

    def _real_assistant(self, message: Message) -> bool:
        if message.kind != RecordType.ASSISTANT:
            return False

        return not message.is_api_error

    def _has_assistant(self, messages: Sequence[Message]) -> bool:
        for message in messages:
            if message.kind == RecordType.ASSISTANT:
                return True

        return False

    def _end(self) -> LadderEnd:
        if self._attempts > 0:
            return LadderEnd.EXHAUSTED

        return LadderEnd.TOO_FEW_GROUPS

    def _without_progress(self, messages: Sequence[Message]) -> list[Message]:
        result: list[Message] = []
        for message in messages:
            if message.kind == "progress":
                continue
            result.append(message)

        return result

    def _flat(self, groups: Sequence[Sequence[Message]]) -> list[Message]:
        result: list[Message] = []
        for group in groups:
            result.extend(group)

        return result


class SummaryPrompt:
    """Текст инструкции суммаризации (kle @209350939) и разбор ответа (QFo @209351120).

    Текст базовой инструкции читается из файла рядом с модулем; пользовательские
    инструкции (/compact <текст> и stdout хука PreCompact) вставляются перед
    финальным напоминанием.
    """

    REMINDER = "\n\nREMINDER:"
    ADDITIONAL = "\n\nAdditional Instructions:\n"

    def __init__(self, base_path: Path) -> None:
        try:
            self._base = base_path.read_text(encoding=Encoding.UTF8)
        except OSError as exc:
            raise CompactionError(f"reading summarization prompt {base_path} failed: {exc}") from exc

        self._split = self._base.rfind(self.REMINDER)
        if self._split < 0:
            raise CompactionError(f"{base_path}: expected the closing 'REMINDER:' paragraph in the summarization prompt, found none")

        self._arguments = re.compile(CompactMark.COMMAND_ARGS, re.DOTALL)
        self._analysis = re.compile(CompactMark.ANALYSIS)
        self._summary = re.compile(CompactMark.SUMMARY)
        self._blank_run = re.compile(CompactMark.BLANK_RUN)

    def render(self, custom: str) -> str:
        if not custom.strip():
            return self._base

        return self._base[: self._split] + self.ADDITIONAL + custom + self._base[self._split :]

    def custom_of(self, command: str) -> str:
        """Аргумент команды /compact из её записи в журнале."""
        found = self._arguments.search(command)
        if found is None:
            return ""

        return found.group(1)

    def merge(self, argument: str, hook_output: str) -> str:
        """lYe: инструкция пользователя и вывод хука PreCompact через пустую строку."""
        if not hook_output:
            return argument

        if not argument:
            return hook_output

        return f"{argument}\n\n{hook_output}"

    def clean(self, reply: str) -> str:
        """Ответ модели → текст сводки: <analysis> удаляется, <summary>X</summary> → «Summary:\\nX»."""
        text = self._analysis.sub("", reply, count=1)
        match = self._summary.search(text)

        if match is not None:
            text = text[: match.start()] + "Summary:\n" + match.group(1).strip() + text[match.end() :]

        return self._blank_run.sub("\n\n", text).strip()


class SummaryWrapper:
    """Обёртка сводки — содержимое записи isCompactSummary (LY @209351548)."""

    HEAD = (
        "This session is being continued from a previous conversation that ran out of context. "
        "The summary below covers the earlier portion of the conversation.\n\n"
    )
    TRANSCRIPT = (
        "\n\nIf you need specific details from before compaction (like exact code snippets, error messages, "
        "or content you generated), read the full transcript at: "
    )
    HEAD_TRUNCATED = (
        "\n\nNote: the earliest part of the conversation was too large to include and is NOT covered by this summary"
        " (the full transcript mentioned above still has it). If the task turns out to depend on something from that part,"
        " say so plainly rather than guessing at it."
    )
    CONTINUE = (
        "\nContinue the conversation from where it left off without asking the user any further questions. "
        "Resume directly — do not acknowledge the summary, do not recap what was happening, do not preface with "
        '"I\'ll continue" or similar. Pick up the last task as if the break never happened.'
    )

    def wrap(self, summary: str, transcript_path: str, head_truncated: bool, suppress_follow_up: bool) -> str:
        text = self.HEAD + summary

        if transcript_path:
            text += self.TRANSCRIPT + transcript_path

        if head_truncated:
            text += self.HEAD_TRUNCATED

        if suppress_follow_up:
            text += self.CONTINUE

        return text

    def unwrap(self, content: str, transcript_path: str) -> str:
        """Текст сводки из содержимого записи; пустая строка, если обёртка не та."""
        if not content.startswith(self.HEAD):
            return ""

        tail = self.TRANSCRIPT + transcript_path
        end = content.find(tail)
        if end < 0:
            return ""

        return content[len(self.HEAD) : end]


@dataclass(frozen=True)
class SummaryRequest:
    """Запрос суммаризации: тело, заголовки, отличающие его от основного, и место метки кэша."""

    body: dict[str, Json]
    headers: dict[str, str]
    mark_index: int
    notes: tuple[str, ...]


class SummaryRequestBuilder:
    """Тело запроса суммаризации при сжатии (o$o @209354400 → Wjt @209417570, Gk с querySource compact).

    Запрос в журнал не пишется. Суммаризуется история до сохранённого хвоста,
    к ней дописано user-сообщение с инструкцией; system, tools и параметры —
    как у основного запроса (их даёт RequestBuilder), метки без ttl,
    добавлен fallbacks. Метка кэша в messages отступает на одно помечаемое
    сообщение (skipCacheWrite). Создаётся над RequestBuilder'ом того же
    журнала сверкой и командой compaction.
    """

    def __init__(self, builder: RequestBuilder, launch: LaunchParams, prompt: SummaryPrompt) -> None:
        self._builder = builder
        self._launch = launch
        self._prompt = prompt

    def build(self, cut: int, at: int, custom: str, trigger: Trigger, threads: bool) -> SummaryRequest:
        """Запрос суммаризации истории до строки cut, отправленный в момент строки at.

        cut — первая строка сохранённого хвоста; at — строка, перед которой
        идёт сжатие (запись границы либо запрос): по ней берутся system,
        tools и параметры; threads — слал ли процесс запрос основного цикла.
        """
        notes: list[str] = [
            f"messages: history before line {cut} plus the summary instruction, request state as of line {at}; "
            f"trigger {trigger.value!r}, custom instructions {len(custom)} characters"
        ]
        assembler = self._builder.assembler

        try:
            history: list[Entry] = list(assembler.history_before(cut))
            history.append(UserEntry(uuid=CompactMark.INSTRUCTION_UUID, content=self._prompt.render(custom)))
            moment = replace(assembler.moment_at(cut, 0), effort=self._builder.parts.params.turn_effort(cut))
            entries = assembler.entries(history, moment, notes)
            messages, marks = assembler.serialize(entries, True)
            body = self._builder.parts.compose(
                Composition(
                    line=at,
                    ttl=self._launch.compact_cache_ttl,
                    messages=messages,
                    marks=marks,
                    fallback=self._launch.server_side_fallback,
                    threads=threads,
                    evict=False,
                    edits=(),
                    agentic=False,
                ),
                notes,
            )
        except (AssembleError, RequestError) as exc:
            raise CompactionError(f"building the summary request for history before line {cut} failed: {exc}") from exc

        headers = {
            str(Header.COMPACTION_REQUEST): str(trigger),
            str(Header.COMPACTION): str(trigger),
            str(Header.REQUEST_CLASS): str(Header.CLASS_COMPACTION),
        }
        return SummaryRequest(body=body, headers=headers, mark_index=self._mark_index(marks, entries), notes=tuple(notes))

    def recorded(self, boundary_line: int, threads: bool) -> SummaryRequest:
        """Запрос, приведший к записи границы: хвост и аргумент /compact берутся из журнала."""
        try:
            boundary = self._builder.facts.boundary_at(boundary_line)
        except RequestError as exc:
            raise CompactionError(f"locating the compact boundary for the summary request failed: {exc}") from exc

        cut = boundary.line
        for uuid in boundary.preserved:
            if uuid in self._builder.facts.lines:
                cut = min(cut, self._builder.facts.lines[uuid])

        custom = ""
        end = self._builder.facts.window_end(boundary.line)
        for command in self._builder.facts.commands:
            if boundary.line < command.line < end:
                custom = self._prompt.custom_of(command.names[0])
                break

        return self.build(cut, boundary.line, custom, Trigger(boundary.trigger), threads)

    def _mark_index(self, marks: Sequence[MarkPath], entries: Sequence[Entry]) -> int:
        """Номер сообщения провода с меткой; -1 — метки нет."""
        if not marks or not entries:
            return -1

        position = marks[0][1]
        if isinstance(position, int):
            return position

        return -1


@dataclass(frozen=True)
class BoundaryForecast:
    """Предсказанные поля compactMetadata и связи записи-границы."""

    trigger: Trigger
    pre_tokens: int
    logical_parent: str
    kept_uuids: tuple[str, ...]
    discovered_tools: tuple[str, ...]
    previous_dropped: int

    def cumulative_dropped(self, post_tokens: int) -> int:
        """y0 @208548624: прежний максимум плюс сброшенное этим сжатием."""
        return self.previous_dropped + max(0, self.pre_tokens - post_tokens)


class BoundaryForecaster:
    """Предсказание записи-границы по списку сообщений и выбранной попытке (Zqn @209378200, VU, DNe, uY).

    preTokens считается ContextMeter'ом с делителем 4 — в отличие от проверки
    порога, где делитель берётся из фактов о модели.
    """

    def __init__(self) -> None:
        self._estimator = TokenEstimator(int(Limit.REPORT_DIVISOR))
        self._meter = ContextMeter(self._estimator)

    def forecast(self, messages: Sequence[Message], attempt: Attempt, trigger: Trigger, surfaced: frozenset[str]) -> BoundaryForecast:
        """surfaced — имена отложенных инструментов, уже ушедших на провод в этом процессе."""
        if not messages:
            raise CompactionError("boundary forecast expects a non-empty message list, got 0 messages")

        kept: list[str] = []
        for message in attempt.to_keep:
            kept.append(message.uuid)

        return BoundaryForecast(
            trigger=trigger,
            pre_tokens=self._meter.tokens(messages),
            logical_parent=messages[-1].uuid,
            kept_uuids=tuple(kept),
            discovered_tools=tuple(sorted(set(self._discovered(messages, surfaced)))),
            previous_dropped=self._previous_dropped(messages),
        )

    def post_tokens(self, summary: Message, kept: Sequence[Message], attachments: Sequence[Message]) -> int:
        """qm(mD(result)): сводка, хвост, вложения и результаты хуков; граница даёт 0."""
        return self._estimator.messages([summary, *kept, *attachments])

    def _previous_dropped(self, messages: Sequence[Message]) -> int:
        best = 0

        for message in messages:
            if not message.is_boundary:
                continue

            metadata = message.metadata
            dropped = metadata.get("cumulativeDroppedTokens")

            if not isinstance(dropped, int):
                dropped = self._own_drop(metadata)

            best = max(best, dropped)

        return best

    def _own_drop(self, metadata: Fields) -> int:
        pre = metadata.get("preTokens")
        post = metadata.get("postTokens")

        if not isinstance(pre, int):
            return 0

        if not isinstance(post, int):
            return max(0, pre)

        return max(0, pre - post)

    def surfaced_names(self, messages: Sequence[Message]) -> frozenset[str]:
        """Имена из surfacedNames вложений deferred_tools_delta."""
        names: set[str] = set()
        for message in messages:
            if message.attachment_kind != "deferred_tools_delta":
                continue
            names.update(self._strings(message.attachment.get("surfacedNames")))

        return frozenset(names)

    def _discovered(self, messages: Sequence[Message], surfaced: frozenset[str]) -> Iterator[str]:
        for message in messages:
            if message.is_boundary:
                yield from self._strings(message.metadata.get("preCompactDiscoveredTools"))
                continue

            if message.attachment_kind == "deferred_tools_delta":
                for name in self._strings(message.attachment.get("surfacedNames")):
                    if name in surfaced:
                        yield name
                continue

            if message.kind != RecordType.USER:
                continue

            for block in message.blocks:
                yield from self._references(block)

    def _references(self, block: Fields) -> Iterator[str]:
        if block.get("type") != BlockType.TOOL_RESULT:
            return

        content = block.get("content")
        if not isinstance(content, list):
            return

        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != BlockType.TOOL_REFERENCE:
                continue
            name = item.get("tool_name")
            if isinstance(name, str):
                yield name

    def _strings(self, value: Json) -> Iterator[str]:
        if not isinstance(value, list):
            return

        for item in value:
            if isinstance(item, str):
                yield item


@dataclass(frozen=True)
class CompactedRecords:
    """Записи сжатия по ролям: то, что клиент выдаёт наружу и держит в памяти."""

    boundary: Message
    context: tuple[Message, ...]
    summary: Message
    kept: tuple[Message, ...]
    attachments: tuple[Message, ...]

    def in_memory(self) -> list[Message]:
        """On(mD(result)): граница, контекст хода, сводка, хвост, вложения и результаты хуков."""
        return [self.boundary, *self.context, self.summary, *self.kept, *self.attachments]

    def journaled(self) -> list[Message]:
        """m0r: то же без хвоста — он уже лежит в журнале до границы."""
        return [self.boundary, *self.context, self.summary, *self.attachments]


class PostCompactLayout:
    """Порядок записей после границы и проверка порядка вложений (Cle @209424900, mD, m0r).

    Вложения идут группами: файлы, фоновые задачи, очередь, файл плана, режим
    плана, вызванные скиллы, дельты и окружение, результаты хука
    SessionStart(compact).
    """

    def __init__(self) -> None:
        self._ranks: dict[str, AttachmentRank] = {
            "file": AttachmentRank.FILE,
            "compact_file_reference": AttachmentRank.FILE,
            "task_status": AttachmentRank.TASK_STATUS,
            "queued_command": AttachmentRank.QUEUED_COMMAND,
            "plan_file_reference": AttachmentRank.PLAN_FILE,
            "plan_mode": AttachmentRank.PLAN_MODE,
            "invoked_skills": AttachmentRank.INVOKED_SKILLS,
            "hook_success": AttachmentRank.HOOK,
            "hook_additional_context": AttachmentRank.HOOK,
        }

    def rank(self, message: Message) -> AttachmentRank:
        kind = message.attachment_kind
        if kind in self._ranks:
            return self._ranks[kind]

        return AttachmentRank.DELTA

    def split(self, after_boundary: Sequence[Message], kept: Sequence[Message]) -> CompactedRecords:
        """Разложить записи журнала после границы по ролям; вложения стадии запроса отбрасываются."""
        if not after_boundary or not after_boundary[0].is_boundary:
            raise CompactionError("post-compact layout expects the boundary record first, got another record")

        boundary = after_boundary[0]
        context: list[Message] = []
        attachments: list[Message] = []
        summary: Message | None = None

        for message in after_boundary[1:]:
            if summary is None and message.is_summary:
                summary = message
                continue

            if summary is None:
                context.append(message)
                continue

            if message.kind != RecordType.ATTACHMENT:
                continue

            if message.is_request_stage:
                break

            attachments.append(message)

        if summary is None:
            raise CompactionError(f"boundary at line {boundary.line}: expected a summary record after it, found none")

        return CompactedRecords(boundary=boundary, context=tuple(context), summary=summary, kept=tuple(kept), attachments=tuple(attachments))

    def ordered(self, attachments: Sequence[Message]) -> bool:
        previous = AttachmentRank.FILE

        for message in attachments:
            rank = self.rank(message)
            if rank < previous:
                return False
            previous = rank

        return True


@dataclass(frozen=True)
class ReadState:
    """Запись readFileState: путь и метка времени, по которой сортируются файлы."""

    path: str
    timestamp: float


class FileRestorePlanner:
    """Выбор файлов, которые подмешиваются заново после сжатия (z$o @209424900).

    Берутся пять записей readFileState с наибольшей меткой времени. Метка —
    mtime файла на момент чтения (@208443701: timestamp:Math.floor(mtimeMs)),
    а не время вызова Read. Файл, не уложившийся в 5000 токенов, заменяется
    ссылкой compact_file_reference.
    """

    def select(self, states: Sequence[ReadState], excluded: Sequence[str]) -> list[ReadState]:
        skip = frozenset(excluded)
        candidates: list[ReadState] = []

        for state in states:
            if state.path in skip:
                continue
            candidates.append(state)

        candidates.sort(key=self._newest_first)
        return candidates[: int(Limit.RESTORED_FILES)]

    def kind(self, tokens: int) -> str:
        if tokens > Limit.RESTORED_FILE_TOKENS:
            return "compact_file_reference"

        return "file"

    def read_paths(self, messages: Sequence[Message]) -> list[str]:
        """Пути вызовов Read в сообщениях — в порядке вызова, без повторов."""
        result: list[str] = []

        for message in messages:
            if message.kind != RecordType.ASSISTANT:
                continue
            for block in message.blocks:
                path = self._read_path(block)
                if path and path not in result:
                    result.append(path)

        return result

    def _read_path(self, block: Fields) -> str:
        if block.get("type") != BlockType.TOOL_USE:
            return ""

        if block.get("name") != CompactMark.READ_TOOL:
            return ""

        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            return ""

        path = tool_input.get("file_path")
        if isinstance(path, str):
            return path

        return ""

    def _newest_first(self, state: ReadState) -> float:
        return -state.timestamp


@dataclass(frozen=True)
class ToolLimits:
    """Декларация инструмента, от которой зависит вынос результата."""

    name: str
    max_result_chars: float
    ceiling: int = int(Limit.PERSIST_CEILING)
    skips_budget: bool = False


class ResultPersistence:
    """Вынос большого результата инструмента в файл и текст заглушки (Due @207035000, q, Fbe @207036700).

    Общий механизм: результат длиннее min(maxResultSizeChars, 50 000) пишется
    в <sessionDir>/tool-results/<tool_use_id>.txt (массив блоков — .json,
    JSON с отступом 2) и заменяется заглушкой. Bash выносит сам при выводе
    длиннее 30 000 символов, файл называется по id задачи оболочки.
    """

    def threshold(self, tool: ToolLimits) -> float:
        if not math.isfinite(tool.max_result_chars):
            return math.inf

        return min(tool.max_result_chars, tool.ceiling)

    def persists(self, tool: ToolLimits, size: int) -> bool:
        return size > self.threshold(tool)

    def bash_persists(self, size: int) -> bool:
        return size > Limit.BASH_INLINE

    def file_name(self, identifier: str, is_block_list: bool) -> str:
        if is_block_list:
            return f"{identifier}.json"

        return f"{identifier}.txt"

    def stored_text(self, content: Json) -> str:
        """Что пишется в файл: строка как есть, массив блоков — JSON с отступом 2."""
        if isinstance(content, str):
            return content

        return json.dumps(content, ensure_ascii=False, indent=2)

    def stub(self, path: str, stored: str) -> str:
        preview, has_more = self._preview(stored)
        text = f"{CompactMark.PERSISTED_OPEN}\n"
        text += f"Output too large ({self.size_label(self._units(stored))}). Full output saved to: {path}\n\n"
        text += f"Preview (first {self.size_label(int(Limit.PREVIEW_CHARS))}):\n"
        text += preview

        if has_more:
            text += "\n...\n"
        else:
            text += "\n"

        return text + CompactMark.PERSISTED_CLOSE

    def over_budget(self, sizes: Sequence[int], enabled: bool) -> bool:
        """Суммарный бюджет группы результатов (ie @207042600) действует только при флаге tengu_hawthorn_steeple."""
        if not enabled:
            return False

        return sum(sizes) > Limit.MESSAGE_BUDGET

    def size_label(self, size: int) -> str:
        kilo = size / 1024
        if kilo < 1:
            return f"{size} bytes"

        if kilo < 1024:
            return f"{self._trim(kilo)}KB"

        mega = kilo / 1024
        if mega < 1024:
            return f"{self._trim(mega)}MB"

        return f"{self._trim(mega / 1024)}GB"

    def _preview(self, stored: str) -> tuple[str, bool]:
        """W9e: до 2000 символов, обрезка по последнему переводу строки, если он дальше середины."""
        limit = int(Limit.PREVIEW_CHARS)
        if self._units(stored) <= limit:
            return stored, False

        head = stored[:limit]
        newline = head.rfind("\n")
        if newline > limit * 0.5:
            return stored[:newline], True

        return head, True

    def _units(self, value: str) -> int:
        return len(value.encode(Encoding.UTF16_LE, Encoding.SURROGATES)) // 2

    def _trim(self, value: float) -> str:
        text = f"{value:.1f}"
        if text.endswith(".0"):
            return text[:-2]

        return text


@dataclass(frozen=True)
class IdleReturn:
    """Возврат после простоя: где он в истории и что о нём известно (pr @203570300)."""

    index: int
    idle_seconds: int
    clearable_tool_uses: int
    trigger_tool_uses: int
    exclude_tools: tuple[str, ...]
    replies_since_idle: int
    server_cleared_since_idle: bool


@dataclass(frozen=True)
class _ToolScan:
    clearable: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    counted: list[str] = field(default_factory=lambda: list[str]())
    excluded: set[str] = field(default_factory=lambda: set[str]())
    replies: set[str] = field(default_factory=lambda: set[str]())


class IdleClearingPlanner:
    """Правка clear_tool_uses_20250919 после простоя дольше 65 минут (fKo @203569445, pr, mr).

    mode — значение флага tengu_zany_pike: "on", "shadow", "off" либо
    "unknown", когда значения нет. При "unknown" правка уходит только в
    сессии, где сервер уже чистил результаты, и только на запросах после
    первого ответа, отмеченного очисткой.
    """

    def __init__(self, mode: str) -> None:
        self._mode = mode
        self._name = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
        self._estimator = TokenEstimator(int(Limit.REPORT_DIVISOR))

    def plan(self, messages: Sequence[Message]) -> dict[str, Json] | None:
        if self._mode == "off":
            return None

        if self._mode == "unknown" and not self._any_cleared(messages):
            return None

        found = self.find_return(messages)
        if found is None:
            return None

        if not self._continues(found):
            return None

        excluded: list[Json] = []
        for name in found.exclude_tools:
            excluded.append(str(name))
        return {
            "type": str(IdleEdit.TYPE),
            "trigger": {"type": str(IdleEdit.TOOL_USES), "value": found.trigger_tool_uses},
            "keep": {"type": str(IdleEdit.TOOL_USES), "value": int(Limit.IDLE_KEEP)},
            "clear_at_least": {"type": str(IdleEdit.INPUT_TOKENS), "value": int(Limit.IDLE_CLEAR_AT_LEAST)},
            "exclude_tools": excluded,
        }

    def find_return(self, messages: Sequence[Message]) -> IdleReturn | None:
        located = self._locate(messages)
        if located is None:
            return None

        index, idle_ms = located
        scan = self._scan(messages, index)
        clearable = len(scan.counted)
        trigger = clearable - int(Limit.IDLE_BELOW)

        if trigger <= Limit.IDLE_KEEP:
            return None

        if trigger < Limit.IDLE_MIN_TRIGGER:
            return None

        return IdleReturn(
            index=index,
            idle_seconds=round(idle_ms / 1000),
            clearable_tool_uses=clearable,
            trigger_tool_uses=trigger,
            exclude_tools=tuple(sorted(scan.excluded)),
            replies_since_idle=len(scan.replies),
            server_cleared_since_idle=self._last_reply_cleared(messages, index),
        )

    def _continues(self, found: IdleReturn) -> bool:
        """mr: первый запрос после простоя — только при "on"; дальше — если сервер уже чистил."""
        if self._mode == "shadow":
            return False

        if found.replies_since_idle == 0:
            return self._mode == "on"

        return found.server_cleared_since_idle

    def _locate(self, messages: Sequence[Message]) -> tuple[int, float] | None:
        limit_ms = int(Limit.IDLE_SECONDS) * 1000
        latest: float | None = None
        after_reply = False
        found: tuple[int, float] | None = None

        for position, message in enumerate(messages):
            stamp = self._stamp(message)
            if stamp is None:
                continue

            if after_reply and latest is not None and stamp - latest > limit_ms and self._is_prompt(message):
                found = (position, stamp - latest)

            if message.kind == RecordType.ASSISTANT:
                after_reply = isinstance(message.raw.get("apiBlockIndex"), int)

            if latest is None or stamp > latest:
                latest = stamp

        return found

    def _scan(self, messages: Sequence[Message], idle_index: int) -> _ToolScan:
        scan = _ToolScan()
        for name in AlwaysExcluded:
            scan.excluded.add(str(name))
        pending: set[str] = set()
        clearable_names = frozenset(Clearable)

        for position, message in enumerate(messages):
            if message.kind == RecordType.ASSISTANT and position >= idle_index and message.usage is not None:
                scan.replies.add(message.reply_id)

            for block in message.blocks:
                kind = block.get("type")

                if kind == BlockType.TOOL_USE:
                    self._note_use(block, position < idle_index, clearable_names, scan, pending)

                if kind == BlockType.TOOL_RESULT and position < idle_index:
                    self._note_result(block, scan, pending)

        return scan

    def _note_use(self, block: Fields, before_idle: bool, clearable: frozenset[Clearable], scan: _ToolScan, pending: set[str]) -> None:
        name = block.get("name")
        identifier = block.get("id")

        if not isinstance(name, str) or not isinstance(identifier, str):
            return

        if name not in clearable:
            if self._name.match(name):
                scan.excluded.add(name)
            return

        if identifier in scan.clearable:
            return

        scan.clearable[identifier] = name
        if before_idle:
            pending.add(identifier)

    def _note_result(self, block: Fields, scan: _ToolScan, pending: set[str]) -> None:
        identifier = block.get("tool_use_id")
        if not isinstance(identifier, str):
            return

        if identifier not in pending:
            return

        pending.discard(identifier)
        if self._big_enough(block.get("content")):
            scan.counted.append(identifier)

    def _big_enough(self, content: Json) -> bool:
        """Xl: результат считается, если в нём не меньше 64 символов текста либо есть медиа."""
        if isinstance(content, str):
            return len(content) >= Limit.IDLE_MIN_CHARS

        if not isinstance(content, list):
            return False

        chars = 0
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") in (BlockType.IMAGE, BlockType.DOCUMENT):
                return True
            text = item.get("text")
            if isinstance(text, str):
                chars += len(text)

        return chars >= Limit.IDLE_MIN_CHARS

    def _last_reply_cleared(self, messages: Sequence[Message], idle_index: int) -> bool:
        cleared = False

        for message in messages[idle_index:]:
            if message.kind != RecordType.ASSISTANT:
                continue
            if message.usage is None:
                continue
            if message.message.get("stop_reason") is None:
                continue
            cleared = self._cleared(message)

        return cleared

    def _any_cleared(self, messages: Sequence[Message]) -> bool:
        for message in messages:
            if self._cleared(message):
                return True

        return False

    def _cleared(self, message: Message) -> bool:
        """xt: ответ несёт applied_edits с cleared_tool_uses > 0."""
        if message.kind != RecordType.ASSISTANT:
            return False

        management = message.message.get("context_management")
        if not isinstance(management, dict):
            return False

        edits = management.get("applied_edits")
        if not isinstance(edits, list):
            return False

        for edit in edits:
            if not isinstance(edit, dict):
                continue
            if edit.get("type") != IdleEdit.TYPE:
                continue
            count = edit.get("cleared_tool_uses")
            if isinstance(count, int) and count > 0:
                return True

        return False

    def _is_prompt(self, message: Message) -> bool:
        if message.kind != RecordType.USER:
            return False

        return not message.has_tool_result

    def _stamp(self, message: Message) -> float | None:
        """xn: время записи; синтетика, мета-записи и сводка времени не дают."""
        if message.kind == RecordType.ASSISTANT:
            if message.model == Synthetic.MODEL or message.is_api_error:
                return None
        elif message.kind == RecordType.USER:
            if message.is_meta or message.is_summary or message.raw.get("isVisibleInTranscriptOnly") is True:
                return None
        else:
            return None

        text = message.timestamp
        if not text:
            return None

        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            return None


class IdleClearing(ContextEdits):
    """Реализация ContextEdits: правка clear_tool_uses для строки запроса.

    Связывает IdleClearingPlanner со списком сообщений клиента
    (CompactionLog); RequestBuilder спрашивает у неё правку
    context_management, от которой зависят ещё нить и вторая метка кэша.
    """

    def __init__(self, log: CompactionLog, mode: str) -> None:
        self._log = log
        self._planner = IdleClearingPlanner(mode)
        self._known: dict[int, Mapping[str, Json] | None] = {}

    def tool_clearing(self, line: int) -> Mapping[str, Json] | None:
        if line in self._known:
            return self._known[line]

        try:
            planned = self._planner.plan(self._log.window_before(line))
        except CompactionError as exc:
            raise RequestError(f"planning the tool clearing edit for the request at line {line} failed: {exc}") from exc

        self._known[line] = planned
        return planned


class CompactionModel:
    """Фасад модели: решение перед запросом и план сжатия по списку сообщений.

    Собирается из порога (ThresholdPolicy) и делителя оценки модели; создаёт
    счётчики, шлюз решения и предсказатель границы. Создаётся PromptSession;
    им пользуются CompactionReplay и сверка.
    """

    def __init__(self, policy: ThresholdPolicy, chars_per_token: int) -> None:
        self.policy = policy
        self.meter = ContextMeter(TokenEstimator(chars_per_token))
        self.gate = AutoCompactGate(policy, self.meter)
        self.forecaster = BoundaryForecaster()
        self.layout = PostCompactLayout()

    def decide(self, messages: Sequence[Message], tracking: CompactTracking, query_source: str) -> Decision:
        return self.gate.decide(messages, tracking, query_source)

    def first_attempt(self, messages: Sequence[Message], allow_last_resort: bool) -> Attempt | LadderStop:
        return SummarizeLadder(messages, allow_last_resort).first()

    def manual_attempt(self, messages: Sequence[Message]) -> Attempt | LadderStop:
        """/compact: та же лестница, последний рубеж разрешён (allowLastResort по умолчанию)."""
        return SummarizeLadder(messages, True).first()


@dataclass(frozen=True)
class Check:
    """Проверка порога перед запросом: состояние истории и что случилось по журналу."""

    line: int
    state: tuple[Message, ...]
    boundary: Message | None
    new_turn: bool
    terminal: bool


@dataclass(frozen=True)
class CheckOutcome:
    """Итог проверки по модели: заполненность, решение, попытка сжатия и исход."""

    check: Check
    fill: Fill
    decision: Decision
    attempt: Attempt | LadderStop | None
    outcome: str


class CheckTimeline:
    """Раскладка журнала на проверки порога: по одной перед каждым запросом и перед концом хода по ошибке.

    Строки запросов приходят снаружи: моменты, найденные по истории, либо
    строки записей эталона. Проверка, после которой клиент сжал историю,
    получает запись-границу; её состояние — список сообщений до границы.
    """

    def __init__(self, log: CompactionLog, request_lines: Sequence[int]) -> None:
        self._log = log
        self._request_lines = tuple(request_lines)

    def checks(self) -> list[Check]:
        result: list[Check] = []
        previous = 0

        for line in self._moments():
            boundary = self._auto_boundary(previous, line)
            state_line = line
            if boundary is not None:
                state_line = boundary.line

            state = self._log.window_before(state_line)
            result.append(
                Check(
                    line=line,
                    state=tuple(state),
                    boundary=boundary,
                    new_turn=self._has_prompt(previous, state_line),
                    terminal=line not in self._request_lines,
                )
            )
            previous = line

        return result

    def _moments(self) -> list[int]:
        lines = list(self._request_lines)

        for message in self._log.messages:
            if message.kind != RecordType.ASSISTANT:
                continue
            if message.model != Synthetic.MODEL:
                continue
            if not message.is_api_error:
                continue
            lines.append(message.line)

        return sorted(lines)

    def _auto_boundary(self, after: int, before: int) -> Message | None:
        for message in self._log.boundaries():
            if message.line <= after or message.line >= before:
                continue
            if message.metadata.get("trigger") == Trigger.AUTO:
                return message

        return None

    def _has_prompt(self, after: int, before: int) -> bool:
        for message in self._log.messages:
            if message.line <= after or message.line >= before:
                continue
            if message.kind != RecordType.USER:
                continue
            if message.is_meta or message.is_summary or message.has_tool_result:
                continue
            return True

        return False


class CompactionReplay:
    """Проигрывает проверки порога по истории, ведя compactTracking, как цикл запроса клиента.

    Счётчики заводятся заново в начале каждого хода (@215800706) и растут
    после каждой итерации с инструментами (@215862795). Им пользуются
    команда compaction и сверка.
    """

    def __init__(self, model: CompactionModel, timeline: CheckTimeline, query_source: str) -> None:
        self._model = model
        self._timeline = timeline
        self._source = query_source

    def outcomes(self) -> list[CheckOutcome]:
        tracking = CompactTracking()
        result: list[CheckOutcome] = []
        first = True

        for check in self._timeline.checks():
            if check.new_turn:
                tracking = CompactTracking()

            if not check.new_turn and not first:
                tracking = tracking.after_tool_round()

            first = False
            decision = self._model.decide(check.state, tracking, self._source)
            attempt: Attempt | LadderStop | None = None
            if decision.compacts:
                attempt = SummarizeLadder(check.state, decision.allow_last_resort).first()

            result.append(
                CheckOutcome(
                    check=check,
                    fill=self._model.meter.fill(check.state),
                    decision=decision,
                    attempt=attempt,
                    outcome=self._outcome(check, decision, attempt),
                )
            )

            if isinstance(attempt, LadderStop):
                tracking = tracking.after_failure()

            if isinstance(attempt, Attempt):
                tracking = tracking.after_success(decision.rapid_refills)

        return result

    def _outcome(self, check: Check, decision: Decision, attempt: Attempt | LadderStop | None) -> str:
        if decision.kind is DecisionKind.RAPID_REFILL_TRIPPED:
            return "turn ends: autocompact_thrashing"

        if isinstance(attempt, Attempt):
            return "compacted"

        if self._model.gate.blocks(check.state, False, self._source):
            return f"turn ends: {CompactMark.PROMPT_TOO_LONG}"

        return "request sent"
