"""Модели записей журнала сессии Claude Code 2.1.289 и чтение файла журнала.

Нижний слой пакета: словарь имён полей и типов записей, строка цепочки Row,
адрес журнала (каталог проекта + id сессии), сам журнал, прочитанный один раз,
и строковые операции с семантикой JavaScript. Остальные слои получают журнал
объектом Journal и файл сами не читают.

Ошибки:
JournalError — файл журнала не читается, номер строки состояния лежит за
    концом файла либо путь не похож на журнал сессии; текст называет файл
    и причину.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Iterator, Mapping

Json = None | bool | int | float | str | list["Json"] | dict[str, "Json"]
Raw = Mapping[str, Json]


class JournalError(Exception):
    """Журнал не прочитан."""


class Encoding(StrEnum):
    """Кодировки и режим ошибок: журнал хранит одиночные суррогаты как есть."""

    UTF8 = "utf-8"
    UTF16_LE = "utf-16-le"
    UTF16_BE = "utf-16-be"
    SURROGATES = "surrogatepass"


class Synthetic(StrEnum):
    """Признаки записей, которые клиент создал сам, без ответа API."""

    MODEL = "<synthetic>"


class Layout(StrEnum):
    """Раскладка журналов на диске: каталог проектов, расширение, файлы субагентов."""

    PROJECTS = ".claude/projects"
    SUFFIX = ".jsonl"
    SUBAGENTS = "subagents"


class RecordType(StrEnum):
    """Типы строк журнала."""

    USER = "user"
    ASSISTANT = "assistant"
    ATTACHMENT = "attachment"
    SYSTEM = "system"
    REQUEST = "api-request"
    PROGRESS = "progress"
    SUMMARY = "summary"
    LAST_PROMPT = "last-prompt"
    CONTENT_REPLACEMENT = "content-replacement"
    FILE_SNAPSHOT = "file-history-snapshot"
    ENDED_BY_MODEL = "ended-by-model"
    HISTORY_SUPPRESSION = "history-suppression"
    COST_STATE = "cost-state"
    ATIS_LATCH = "atis-latch"
    SHAPE = "api-request-shape"
    BLOB = "api-request-blob"
    FORK_REF = "fork-context-ref"
    QUEUE = "queue-operation"

    def in_transcript(self) -> bool:
        return self in (RecordType.USER, RecordType.ASSISTANT, RecordType.ATTACHMENT, RecordType.SYSTEM)


class Key(StrEnum):
    """Имена полей строк журнала, блоков содержимого и тела запроса."""

    TYPE = "type"
    TEXT = "text"
    ROLE = "role"
    CONTENT = "content"
    ID = "id"
    NAME = "name"
    INPUT = "input"
    TOOL = "tool"
    TOOL_USE_ID = "tool_use_id"
    IS_ERROR = "is_error"
    SIGNATURE = "signature"
    CITATIONS = "citations"
    CACHE_CONTROL = "cache_control"
    OUTPUT_CONFIG = "output_config"
    CLEAR_AT = "clear_at"
    EFFORT = "effort"
    MESSAGES = "messages"
    UUID = "uuid"
    PARENT = "parentUuid"
    SIDECHAIN = "isSidechain"
    TIMESTAMP = "timestamp"
    MESSAGE = "message"
    MODEL = "model"
    SUBTYPE = "subtype"
    SESSION = "sessionId"
    ATTACHMENT = "attachment"
    LEAF = "leafUuid"
    EXPLICIT = "explicit"
    REWOUND = "rewound"
    META = "isMeta"
    AGENT = "agentId"
    SOURCE_ASSISTANT = "sourceToolAssistantUUID"
    COMPACT_METADATA = "compactMetadata"
    API_ERROR = "isApiErrorMessage"
    VIRTUAL = "isVirtual"
    SHUTDOWN = "interruptedByShutdown"
    REQUEST_ID = "requestId"
    SYSTEM = "system"
    TOOLS = "tools"
    BETAS = "betas"
    MAX_TOKENS = "max_tokens"
    THINKING = "thinking"
    DISPLAY = "display"
    CONTEXT_MANAGEMENT = "context_management"
    EDITS = "edits"
    KEEP = "keep"
    FALLBACKS = "fallbacks"
    SAFEGUARDS = "safeguards"
    NAME_ONLY = "nameOnlyAnnouncements"
    TTL = "ttl"
    SCOPE = "scope"
    EVICT = "evict_on_complete"
    DEFER_LOADING = "defer_loading"
    DESCRIPTION = "description"
    INPUT_SCHEMA = "input_schema"
    PROPERTIES = "properties"
    SCHEMA = "schema"
    SYSTEM_PROMPT = "systemPrompt"
    CLI_PREFIX = "cliPrefix"
    SYSTEM_TURNS = "systemTurns"
    TOOL_CHANGE_HEADER = "toolChangeHeader"
    INLINE_TOOLS = "inlineTools"
    KEPT_REMINDERS = "keptReminders"
    ENTRIES = "entries"
    ADDED_NAMES = "addedNames"
    REMOVED_NAMES = "removedNames"
    READDED_NAMES = "readdedNames"
    SURFACED_NAMES = "surfacedNames"
    SEARCH_ABSENT = "toolSearchAbsent"
    IDENTITY = "identity"
    MODEL_ID = "modelId"
    DISCOVERED = "preCompactDiscoveredTools"
    PRESERVED = "preservedMessages"
    UUIDS = "uuids"
    TRIGGER = "trigger"
    ADVISOR_MODEL = "advisorModel"
    PER_TURN_EFFORT = "perTurnEffort"
    TOOL_NAME = "tool_name"


class BlockType(StrEnum):
    """Типы блоков содержимого сообщения."""

    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    THINKING = "thinking"
    REDACTED_THINKING = "redacted_thinking"
    TOOL_REFERENCE = "tool_reference"
    TOOL_ADDITION = "tool_addition"
    TOOL_REMOVAL = "tool_removal"
    SERVER_TOOL_USE = "server_tool_use"
    MCP_TOOL_USE = "mcp_tool_use"
    ADVISOR_TOOL_RESULT = "advisor_tool_result"
    FALLBACK = "fallback"
    IMAGE = "image"
    DOCUMENT = "document"


class Subtype(StrEnum):
    """Подтипы system-строк и типы вложений с особой ролью при загрузке."""

    COMPACT_BOUNDARY = "compact_boundary"
    LOCAL_COMMAND = "local_command"
    REFUSAL_FALLBACK = "model_refusal_fallback"
    FORK_BRIEFING = "fork_briefing"
    MAX_TURNS = "max_turns_reached"


class AttachmentKind(StrEnum):
    """Типы вложений, которые различает сборка запроса."""

    DEFERRED_TOOLS_DELTA = "deferred_tools_delta"
    QUEUED_COMMAND = "queued_command"
    POLL_EVENTS = "poll_events"
    BATCHING_REMINDER = "batching_reminder"
    SECONDARY_REMINDER = "secondary_reminder"
    BATCHING_REMINDER_SENT = "batching_reminder_sent"
    SECONDARY_REMINDER_SENT = "secondary_reminder_sent"
    PROMPT_SNAPSHOT = "prompt_snapshot"
    DEFERRED_TOOLS_RECORD = "deferred_tools_record"
    MODEL = "model"


@dataclass(frozen=True)
class Row:
    """Строка цепочки (user, assistant, attachment, system) с номером строки файла.

    line равен 0 у записи, которую загрузчик создал сам (синтетический
    tool_result, ответ «No response requested.»).
    """

    line: int
    raw: Raw

    @property
    def kind(self) -> str:
        value = self.raw.get(Key.TYPE)
        if isinstance(value, str):
            return value

        return ""

    @property
    def uuid(self) -> str:
        value = self.raw.get(Key.UUID)
        if isinstance(value, str):
            return value

        return ""

    @property
    def parent(self) -> str | None:
        value = self.raw.get(Key.PARENT)
        if isinstance(value, str) and value:
            return value

        return None

    @property
    def sidechain(self) -> bool:
        return bool(self.raw.get(Key.SIDECHAIN))

    @property
    def timestamp(self) -> str:
        value = self.raw.get(Key.TIMESTAMP)
        if isinstance(value, str):
            return value

        return ""

    @property
    def message(self) -> Raw:
        value = self.raw.get(Key.MESSAGE)
        if isinstance(value, dict):
            return value

        return {}

    @property
    def content(self) -> Json:
        return self.message.get(Key.CONTENT)

    @property
    def blocks(self) -> list[Raw]:
        content = self.content
        if not isinstance(content, list):
            return []

        result: list[Raw] = []
        for block in content:
            if isinstance(block, dict):
                result.append(block)

        return result

    @property
    def message_id(self) -> str | None:
        value = self.message.get(Key.ID)
        if isinstance(value, str) and value:
            return value

        return None

    @property
    def subtype(self) -> str:
        value = self.raw.get(Key.SUBTYPE)
        if isinstance(value, str):
            return value

        return ""

    @property
    def attachment(self) -> Raw:
        value = self.raw.get(Key.ATTACHMENT)
        if isinstance(value, dict):
            return value

        return {}

    @property
    def attachment_kind(self) -> str:
        value = self.attachment.get(Key.TYPE)
        if isinstance(value, str):
            return value

        return ""

    def flag(self, key: str) -> bool:
        return self.raw.get(key) is True

    def text(self, key: str) -> str | None:
        value = self.raw.get(key)
        if isinstance(value, str):
            return value

        return None

    def with_parent(self, parent: str | None) -> Row:
        raw = dict(self.raw)
        raw[Key.PARENT] = parent
        return Row(line=self.line, raw=raw)

    def with_content(self, content: Json) -> Row:
        message = dict(self.message)
        message[Key.CONTENT] = content
        raw = dict(self.raw)
        raw[Key.MESSAGE] = message
        return Row(line=self.line, raw=raw)

    def with_message_id(self, message_id: str) -> Row:
        message = dict(self.message)
        message[Key.ID] = message_id
        raw = dict(self.raw)
        raw[Key.MESSAGE] = message
        return Row(line=self.line, raw=raw)


class JsNumber(float):
    """Дробное число журнала вместе с исходной записью.

    Журнал пишет JSON.stringify, поэтому литерал в строке журнала уже равен
    тому, что выдаст каноническая сериализация JS; python-repr того же числа
    может отличаться (1e-7 против 1e-07). Создаётся разбором строки журнала,
    читается CanonicalJson.
    """

    literal: str

    def __new__(cls, literal: str) -> "JsNumber":
        number = super().__new__(cls, literal)
        number.literal = literal
        return number


class JsonView:
    """Типизированное чтение полей сырой записи журнала."""

    def text(self, holder: Mapping[str, Json], key: str) -> str:
        value = holder.get(key)
        if isinstance(value, str):
            return value

        return ""

    def flag(self, holder: Mapping[str, Json], key: str) -> bool:
        return holder.get(key) is True

    def mapping(self, holder: Mapping[str, Json], key: str) -> Mapping[str, Json]:
        value = holder.get(key)
        if isinstance(value, dict):
            return value

        return {}

    def strings(self, holder: Mapping[str, Json], key: str) -> tuple[str, ...]:
        return tuple(self._strings(holder.get(key)))

    def objects(self, value: Json) -> tuple[Mapping[str, Json], ...]:
        return tuple(self._objects(value))

    def copy(self, value: Json) -> Json:
        if isinstance(value, list):
            items: list[Json] = []
            for item in value:
                items.append(self.copy(item))
            return items

        if isinstance(value, dict):
            copied: dict[str, Json] = {}
            for key, item in value.items():
                copied[key] = self.copy(item)
            return copied

        return value

    def _strings(self, value: Json) -> Iterator[str]:
        if not isinstance(value, list):
            return

        for item in value:
            if isinstance(item, str):
                yield item

    def _objects(self, value: Json) -> Iterator[Mapping[str, Json]]:
        if not isinstance(value, list):
            return

        for item in value:
            if isinstance(item, dict):
                yield item

class JsText:
    """Строковые операции с семантикой JavaScript: trim, пробельность, Date.parse."""

    _SPACES = "\t\n\x0b\x0c\r \xa0                　﻿"
    _TAG_BREAKS = _SPACES + "\x1c\x1d\x1e\x1f\x85"

    def trim(self, text: str) -> str:
        return text.strip(self._SPACES)

    def trim_end(self, text: str) -> str:
        return text.rstrip(self._SPACES)

    def blank(self, text: str) -> bool:
        return self.trim(text) == ""

    def breaks_tag(self, char: str) -> bool:
        if char == "":
            return False

        return char in self._TAG_BREAKS

    def epoch_ms(self, text: str) -> float | None:
        if not text:
            return None

        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None

        return moment.timestamp() * 1000


@dataclass(frozen=True)
class JournalAddress:
    """Адрес журнала: каталог проекта и id сессии.

    Один id сессии встречается в разных каталогах проектов (искусственные
    журналы корпуса), поэтому журнал адресуется парой. У файла субагента
    session — путь от каталога проекта без расширения
    (`<id>/subagents/agent-<agentId>`).
    """

    project: str
    session: str

    def path(self, projects: Path) -> Path:
        return projects / self.project / f"{self.session}{Layout.SUFFIX}"

    @property
    def label(self) -> str:
        return f"{self.project}/{self.session}"


class JournalAddresses:
    """Разбор пути файла журнала в адрес; им пользуются манифест корпуса и вход командной строки."""

    def of_path(self, path: Path) -> JournalAddress:
        if path.suffix != Layout.SUFFIX:
            raise JournalError(f"{path}: a session journal is expected to end with {Layout.SUFFIX}, got {path.suffix!r}")

        resolved = path.resolve()
        holder = resolved.parent
        session = resolved.stem

        if holder.name == Layout.SUBAGENTS:
            session = f"{holder.parent.name}/{Layout.SUBAGENTS}/{resolved.stem}"
            holder = holder.parent.parent

        return JournalAddress(project=holder.name, session=session)


class Journal:
    """Журнал сессии, прочитанный один раз (tRe @211941007, разбор OGe @199972571).

    Хранит строки-объекты с номерами; строки, не являющиеся JSON, пропускает и
    запоминает их номера, строки-необъекты отбрасывает, как это делает
    клиент. Дробные числа читаются JsNumber'ом. Создаётся входом командной
    строки и сверкой, передаётся загрузчику, сборке и декодеру эталона. Файл
    длиннее 5 МиБ клиент читает быстрым путём QSn — он не моделируется.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: list[tuple[int, Raw]] = []
        self._unparsed: list[int] = []
        self._lines = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def line_count(self) -> int:
        return self._lines

    def load(self) -> None:
        try:
            data = self._path.read_bytes()
        except OSError as exc:
            raise JournalError(f"reading session journal {self._path} failed: {exc}") from exc

        rows = data.decode(Encoding.UTF8, Encoding.SURROGATES).split("\n")
        self._lines = len(rows)

        for number, row in enumerate(rows, start=1):
            self._note(number, row)

    def entries(self, upto: int | None = None) -> Iterator[tuple[int, Raw]]:
        """Строки-объекты журнала по порядку; upto — последняя строка состояния (включительно)."""
        if upto is not None and upto > self._lines:
            raise JournalError(f"{self._path}: state line {upto} is past the end of the journal ({self._lines} lines)")

        for number, raw in self._entries:
            if upto is not None and number > upto:
                break
            yield number, raw

    def without(self, kinds: frozenset[str]) -> Journal:
        """Тот же журнал без строк перечисленных типов; номера остальных строк прежние."""
        bare = Journal(self._path)
        bare._lines = self._lines
        bare._unparsed = list(self._unparsed)

        for number, raw in self._entries:
            if raw.get(Key.TYPE) not in kinds:
                bare._entries.append((number, raw))

        return bare

    def unparsed(self, upto: int | None = None) -> int:
        """Сколько строк до upto не разобралось как JSON."""
        count = 0
        for number in self._unparsed:
            if upto is None or number <= upto:
                count += 1

        return count

    def _note(self, number: int, row: str) -> None:
        text = row.strip()
        if not text:
            return

        try:
            parsed: Json = json.loads(text, parse_float=JsNumber)
        except ValueError:
            self._unparsed.append(number)
            return

        if isinstance(parsed, dict):
            self._entries.append((number, parsed))
