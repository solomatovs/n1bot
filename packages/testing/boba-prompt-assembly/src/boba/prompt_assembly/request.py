"""Полное тело запроса к API из записей истории: system, tools, параметры (Claude Code 2.1.289).

Вход — список записей в памяти процесса (SessionMemory) и строка запроса;
выход — тело в том виде, в каком его восстанавливает декодер эталона: model,
system, tools, messages, betas, max_tokens, thinking, context_management,
output_config и метки кэша. Сборку messages выполняет Assembler. Всё, чего в
журнале нет, приходит моделью LaunchParams.

Ошибки:
RequestError — запись истории не разбирается, в журнале нет записи, без
    которой часть тела не строится (prompt_snapshot, deferred_tools_record,
    model), либо внешних параметров запуска не хватает; текст называет
    журнал, строку и причину. Ошибки сборки messages (AssembleError)
    приходят упакованными в неё.
"""

from __future__ import annotations

import sys
from abc import abstractmethod
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Iterator, Mapping, Protocol, Sequence

from boba.prompt_assembly.cache import CacheKind, CacheStamp
from boba.prompt_assembly.entries import AssembleError, Assembled, MarkPath
from boba.prompt_assembly.loader import SessionMemory
from boba.prompt_assembly.messages import Assembler
from boba.prompt_assembly.records import (
    AttachmentKind,
    BlockType,
    Journal,
    JsonView,
    Json,
    Key,
    RecordType,
    Subtype,
    Synthetic,
)



class RequestError(Exception):
    """Единственная ошибка модуля, выходящая наружу."""


class Beta(StrEnum):
    """Беты запроса; порядок членов — порядок добавления в клиенте (QEn, затем mwn)."""

    CLAUDE_CODE = "claude-code-20250219"
    OAUTH = "oauth-2025-04-20"
    CONTEXT_1M = "context-1m-2025-08-07"
    INTERLEAVED_THINKING = "interleaved-thinking-2025-05-14"
    REDACT_THINKING = "redact-thinking-2026-02-12"
    THINKING_TOKEN_COUNT = "thinking-token-count-2026-05-13"
    CONTEXT_MANAGEMENT = "context-management-2025-06-27"
    STRUCTURED_OUTPUTS = "structured-outputs-2025-12-15"
    PROMPT_CACHING_SCOPE = "prompt-caching-scope-2026-01-05"
    MID_CONVERSATION_SYSTEM = "mid-conversation-system-2026-04-07"
    PER_TURN_CONTROL = "per-turn-control-2026-07-01"
    TOOL_CHANGES = "mid-conversation-tool-changes-2026-07-01"
    INLINE_TOOLS = "inline-tools-2026-09-15"
    ADVISOR_TOOL = "advisor-tool-2026-03-01"
    ADVANCED_TOOL_USE = "advanced-tool-use-2025-11-20"
    SYSTEM_CLEAR_AT = "mid-conversation-system-clear-at-2026-08-21"
    EFFORT = "effort-2025-11-24"
    SERVER_SIDE_FALLBACK = "server-side-fallback-2026-07-01"
    FALLBACK_CREDIT = "fallback-credit-2026-06-01"
    DANGEROUS_TOOL_USE = "dangerous-tool-use-2026-09-03"
    THINKING_BINDING = "thinking-binding-controls-2026-08-01"
    THINKING_DISPLAY_UPDATES = "thinking-display-updates-2026-08-18"
    FAST_MODE = "fast-mode-2026-02-01"
    AFK_MODE = "afk-mode-2026-01-31"
    EXTENDED_CACHE_TTL = "extended-cache-ttl-2025-04-11"
    CACHE_EVICT = "prompt-caching-evict-2026-05-12"
    CACHE_DIAGNOSIS = "cache-diagnosis-2026-04-07"
    MESSAGE_THREADS = "message-threads-2026-08-12"


class Wire(StrEnum):
    """Константы клиента, попадающие в тело запроса."""

    BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"
    SECTION_JOIN = "\n\n"
    TOOL_SEARCH = "ToolSearch"
    PLACEHOLDER = "DeferredToolPlaceholder"
    PLACEHOLDER_TEXT = "Reserved placeholder that keeps deferred tool loading active; never call this tool."
    ADVISOR = "advisor"
    ADVISOR_TYPE = "advisor_20260301"
    MCP_PREFIX = "mcp__"
    OBJECT = "object"
    CLEAR_THINKING = "clear_thinking_20251015"
    KEEP_ALL = "all"
    ADAPTIVE = "adaptive"
    ENABLED = "enabled"
    DISPLAY_OMITTED = "omitted"
    DISPLAY_UPDATES = "updates"
    FALLBACKS_DEFAULT = "default"
    HAIKU = "haiku"
    COMPACT_COMMAND = "<command-name>/compact</command-name>"


@dataclass(frozen=True)
class ThinkingParam:
    """Параметр thinking тела: вид и способ показа (пустой display — поля нет)."""

    kind: str
    display: str

    @property
    def active(self) -> bool:
        return self.kind in (Wire.ADAPTIVE, Wire.ENABLED)


@dataclass(frozen=True)
class ModelFacts:
    """Факты каталога модели: предел вывода и беты, зависящие от модели."""

    max_tokens: int
    betas: frozenset[Beta]


@dataclass(frozen=True)
class LaunchParams:
    """Внешние параметры запуска: то, что клиент берёт не из журнала.

    environment_betas — беты, зависящие от входа, провайдера и серверных флагов;
    models — каталог моделей; cli_prefix — префикс для случая, когда снимок его
    ещё не записал; *_cache_ttl — TTL меток по источнику запроса ("" — поля ttl
    нет); system_context — строка systemContext, дописываемая к секциям (iOo);
    safeguards — параметр safeguards серверного классификатора (пусто — нет);
    fork_cache_pin — флаг tengu_basalt_spur: вторая метка кэша у запроса без
    нити; tool_clearing_mode — флаг tengu_zany_pike ("on", "shadow", "off",
    "unknown" — значения нет): серверная очистка результатов после простоя.
    """

    models: Mapping[str, ModelFacts]
    thinking: ThinkingParam
    environment_betas: frozenset[Beta]
    main_cache_ttl: str
    subagent_cache_ttl: str
    compact_cache_ttl: str
    deferred_stub_tool: bool
    advisor_deferred: bool
    subagent_cache_evict: bool
    server_side_fallback: bool
    default_effort: str
    cli_prefix: str
    system_context: str
    safeguards: Sequence[Json]
    fork_cache_pin: bool
    tool_clearing_mode: str


class LaunchProfile(StrEnum):
    SUBSCRIPTION_PRINT = "subscription_print"
    SUBAGENT_CACHE_EVICT = "subscription_print+subagent_cache_evict"
    STREAM_JSON = "subscription_stream_json"
    SERVER_CLASSIFIER = "subscription_print+server_classifier"


class LaunchProfiles:
    """Готовые наборы внешних параметров для сценариев корпуса."""

    def variants(self) -> Sequence[tuple[LaunchProfile, LaunchParams]]:
        """Базовый набор и варианты, отличающиеся флагами запуска и состоянием процесса."""
        base = self.subscription_print()
        return (
            (LaunchProfile.SUBSCRIPTION_PRINT, base),
            (LaunchProfile.SUBAGENT_CACHE_EVICT, replace(base, subagent_cache_evict=True)),
            (LaunchProfile.STREAM_JSON, self.stream_json()),
        )

    def subscription_print(self) -> LaunchParams:
        """Подписка (OAuth), firstParty, `-p --output-format json`."""
        common = frozenset((Beta.INTERLEAVED_THINKING, Beta.THINKING_TOKEN_COUNT, Beta.CONTEXT_MANAGEMENT))
        return LaunchParams(
            models={
                "claude-opus-5-5": ModelFacts(max_tokens=128000, betas=common | {Beta.PER_TURN_CONTROL}),
                "claude-sonnet-4-6": ModelFacts(max_tokens=32000, betas=common),
            },
            thinking=ThinkingParam(kind=Wire.ADAPTIVE, display=Wire.DISPLAY_OMITTED),
            environment_betas=frozenset(
                (
                    Beta.CLAUDE_CODE,
                    Beta.OAUTH,
                    Beta.PROMPT_CACHING_SCOPE,
                    Beta.ADVISOR_TOOL,
                    Beta.THINKING_BINDING,
                    Beta.CACHE_DIAGNOSIS,
                    Beta.MESSAGE_THREADS,
                )
            ),
            main_cache_ttl=CacheKind.TTL_1H,
            subagent_cache_ttl="",
            compact_cache_ttl="",
            deferred_stub_tool=True,
            advisor_deferred=True,
            subagent_cache_evict=False,
            server_side_fallback=True,
            default_effort="",
            cli_prefix="",
            system_context="",
            safeguards=(),
            fork_cache_pin=True,
            tool_clearing_mode="unknown",
        )

    def stream_json(self) -> LaunchParams:
        """Режим расширения VS Code: `--input-format stream-json`, мышление показывается обновлениями."""
        base = self.subscription_print()
        return replace(base, thinking=ThinkingParam(kind=Wire.ADAPTIVE, display=Wire.DISPLAY_UPDATES))

    def server_classifier(self, safeguards: Sequence[Json]) -> LaunchParams:
        """Серверный классификатор действий: параметр safeguards и его беты; контекст — извне."""
        base = self.subscription_print()
        betas = base.environment_betas | {Beta.DANGEROUS_TOOL_USE, Beta.AFK_MODE}
        return replace(base, environment_betas=betas, safeguards=tuple(safeguards))


@dataclass(frozen=True)
class Snapshot:
    """Вложение prompt_snapshot: секции system, инлайн-инструменты, префикс и флаги формы."""

    line: int
    system_prompt: tuple[str, ...]
    tools: tuple[Mapping[str, Json], ...]
    cli_prefix: str
    system_turns: bool
    tool_change_header: bool
    inline_tools: bool
    kept_reminders: bool


@dataclass(frozen=True)
class ToolsDelta:
    """Вложение deferred_tools_delta: изменение пула и объявленные инструменты."""

    line: int
    uuid: str
    added: tuple[str, ...]
    removed: tuple[str, ...]
    surfaced: tuple[str, ...]
    search_absent: bool


@dataclass(frozen=True)
class DeferredRecord:
    """Вложение deferred_tools_record: определения отложенных инструментов, ушедших в tools.

    name_only — uuid дельт, объявленных только текстом: их surfacedNames в tools не едут.
    """

    line: int
    entries: tuple[Mapping[str, Json], ...]
    name_only: tuple[str, ...]


@dataclass(frozen=True)
class Boundary:
    """Запись system/compact_boundary: вид сжатия, обнаруженные инструменты, сохранённый хвост."""

    line: int
    trigger: str
    discovered: tuple[str, ...]
    preserved: tuple[str, ...]


@dataclass(frozen=True)
class AssistantFacts:
    """Поля записи assistant, из которых берутся параметры запроса."""

    line: int
    model: str
    message_id: str
    effort: str
    per_turn_effort: str
    advisor_model: str


@dataclass(frozen=True)
class NamesAt:
    """Имена, встреченные в строке журнала: модель либо ссылки tool_reference."""

    line: int
    names: tuple[str, ...]


@dataclass(frozen=True)
class BuiltRequest:
    """Собранное тело запроса и пояснения, откуда взята каждая часть."""

    line: int
    body: dict[str, Json]
    notes: tuple[str, ...]


class HistoryFacts:
    """Записи истории, из которых строятся system, tools и параметры тела.

    Раскладывает записи журнала по типам один раз; RequestBuilder и
    SummaryRequestBuilder спрашивают у него состояние на строку запроса.
    Повторно записанная строка с уже известным uuid не учитывается. Записи
    api-request не читаются вовсе.
    """

    def __init__(self, journal: Journal) -> None:
        self._path = journal.path
        self._journal = journal
        self._json = JsonView()
        self._seen: set[str] = set()
        self._classified = False
        self.snapshots: list[Snapshot] = []
        self.deltas: list[ToolsDelta] = []
        self.deferred: list[DeferredRecord] = []
        self.boundaries: list[Boundary] = []
        self.assistants: list[AssistantFacts] = []
        self.models: list[NamesAt] = []
        self.references: list[NamesAt] = []
        self.commands: list[NamesAt] = []
        self.lines: dict[str, int] = {}
        self.turn_lines: list[int] = []
        self.sidechain = False

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        for number, raw in self._journal.entries():
            self._note(number, raw)

    def segment_start(self, line: int) -> int:
        """Строка последней границы сжатия перед моментом; 0 — границы не было."""
        start = 0

        for boundary in self.boundaries:
            if boundary.line < line:
                start = boundary.line

        return start

    def answer_line(self, line: int) -> int:
        """Строка первой записи ответа на запрос со строки line; sys.maxsize — ответа в журнале нет."""
        for assistant in self.assistants:
            if assistant.line >= line and assistant.model != Synthetic.MODEL:
                return assistant.line

        return sys.maxsize

    def request_floor(self, line: int) -> int:
        """Строка последней записи user или assistant перед ответом на запрос со строки line.

        Всё, что клиент записал после неё и до ответа (deferred_tools_record),
        дописано самим запросом и при его сборке ещё не было известно.
        """
        answer = self.answer_line(line)
        floor = 0

        for turn in self.turn_lines:
            if turn >= answer:
                break
            floor = turn

        return floor

    def window_end(self, line: int) -> int:
        """Конец окна записей, дописанных запросом: первая запись следующего ответа либо граница.

        Запрос без ответа (отказ API, повтор) окна не закрывает: следующим
        считается ответ с другим message.id после своего. Строка самой границы
        — момент запроса суммаризации: его окно пусто.
        """
        end = sys.maxsize
        own = ""

        for assistant in self.assistants:
            if assistant.line < line or assistant.model == Synthetic.MODEL:
                continue
            if own == "":
                own = assistant.message_id
                continue
            if assistant.message_id != own:
                end = assistant.line
                break

        for boundary in self.boundaries:
            if boundary.line >= line:
                end = min(end, boundary.line)
                break

        return end

    def boundary_at(self, line: int) -> Boundary:
        for boundary in self.boundaries:
            if boundary.line == line:
                return boundary

        raise RequestError(f"{self._path}:{line}: expected a system/compact_boundary record at this line, found none")

    def _note(self, number: int, raw: Mapping[str, Json]) -> None:
        kind = raw.get(Key.TYPE)

        if kind not in (RecordType.USER, RecordType.ASSISTANT, RecordType.ATTACHMENT, RecordType.SYSTEM):
            return

        if uuid := self._json.text(raw, Key.UUID):
            if uuid in self._seen:
                return
            self._seen.add(uuid)
            self.lines[uuid] = number

        if not self._classified:
            self._classified = True
            self.sidechain = self._json.flag(raw, Key.SIDECHAIN)

        if kind in (RecordType.USER, RecordType.ASSISTANT):
            self.turn_lines.append(number)

        if kind == RecordType.ATTACHMENT:
            uuid = self._json.text(raw, Key.UUID)
            self._note_attachment(number, uuid, self._json.mapping(raw, Key.ATTACHMENT))
            return

        if kind == RecordType.ASSISTANT:
            self._note_assistant(number, raw)
            return

        if kind == RecordType.USER:
            self._note_user(number, raw)
            return

        if raw.get(Key.SUBTYPE) == Subtype.COMPACT_BOUNDARY:
            self._note_boundary(number, self._json.mapping(raw, Key.COMPACT_METADATA))

    def _note_attachment(self, number: int, uuid: str, attachment: Mapping[str, Json]) -> None:
        kind = attachment.get(Key.TYPE)

        if kind == AttachmentKind.PROMPT_SNAPSHOT:
            self.snapshots.append(self._snapshot(number, attachment))
            return

        if kind == AttachmentKind.DEFERRED_TOOLS_DELTA:
            self.deltas.append(self._delta(number, uuid, attachment))
            return

        if kind == AttachmentKind.DEFERRED_TOOLS_RECORD:
            entries = self._json.objects(attachment.get(Key.ENTRIES))
            name_only = self._json.strings(attachment, Key.NAME_ONLY)
            self.deferred.append(DeferredRecord(line=number, entries=entries, name_only=name_only))
            return

        if kind != AttachmentKind.MODEL:
            return

        identity = self._json.mapping(attachment, Key.IDENTITY)
        if model := self._json.text(identity, Key.MODEL_ID):
            self.models.append(NamesAt(line=number, names=(model,)))

    def _snapshot(self, number: int, attachment: Mapping[str, Json]) -> Snapshot:
        sections = attachment.get(Key.SYSTEM_PROMPT)
        if not isinstance(sections, list):
            raise RequestError(
                f"{self._path}:{number}: prompt_snapshot expects systemPrompt as a list of strings, got {sections!r}"
            )

        tools: list[Mapping[str, Json]] = []
        for entry in self._json.objects(attachment.get(Key.TOOLS)):
            schema = entry.get(Key.SCHEMA)
            if not isinstance(schema, dict):
                raise RequestError(
                    f"{self._path}:{number}: prompt_snapshot tool {entry.get(Key.NAME)!r} expects schema "
                    f"as an object (the wire entry of the tool), got {schema!r}"
                )
            tools.append(schema)

        return Snapshot(
            line=number,
            system_prompt=self._json.strings(attachment, Key.SYSTEM_PROMPT),
            tools=tuple(tools),
            cli_prefix=self._json.text(attachment, Key.CLI_PREFIX),
            system_turns=self._json.flag(attachment, Key.SYSTEM_TURNS),
            tool_change_header=self._json.flag(attachment, Key.TOOL_CHANGE_HEADER),
            inline_tools=self._json.flag(attachment, Key.INLINE_TOOLS),
            kept_reminders=self._json.flag(attachment, Key.KEPT_REMINDERS),
        )

    def _delta(self, number: int, uuid: str, attachment: Mapping[str, Json]) -> ToolsDelta:
        added = self._json.strings(attachment, Key.ADDED_NAMES) + self._json.strings(attachment, Key.READDED_NAMES)
        return ToolsDelta(
            line=number,
            uuid=uuid,
            added=added,
            removed=self._json.strings(attachment, Key.REMOVED_NAMES),
            surfaced=self._json.strings(attachment, Key.SURFACED_NAMES),
            search_absent=self._json.flag(attachment, Key.SEARCH_ABSENT),
        )

    def _note_assistant(self, number: int, raw: Mapping[str, Json]) -> None:
        if self._json.flag(raw, Key.API_ERROR):
            return

        message = self._json.mapping(raw, Key.MESSAGE)
        self.assistants.append(
            AssistantFacts(
                line=number,
                model=self._json.text(message, Key.MODEL),
                message_id=self._json.text(message, Key.ID),
                effort=self._json.text(raw, Key.EFFORT),
                per_turn_effort=self._json.text(raw, Key.PER_TURN_EFFORT),
                advisor_model=self._json.text(raw, Key.ADVISOR_MODEL),
            )
        )

    def _note_user(self, number: int, raw: Mapping[str, Json]) -> None:
        content = self._json.mapping(raw, Key.MESSAGE).get(Key.CONTENT)

        if isinstance(content, str):
            if Wire.COMPACT_COMMAND in content:
                self.commands.append(NamesAt(line=number, names=(content,)))
            return

        names = tuple(self._referenced(content))
        if names:
            self.references.append(NamesAt(line=number, names=names))

    def _referenced(self, content: Json) -> Iterator[str]:
        """Имена из блоков tool_reference внутри tool_result (uY)."""
        for block in self._json.objects(content):
            if block.get(Key.TYPE) != BlockType.TOOL_RESULT:
                continue
            for inner in self._json.objects(block.get(Key.CONTENT)):
                if inner.get(Key.TYPE) != BlockType.TOOL_REFERENCE:
                    continue
                if name := self._json.text(inner, Key.TOOL_NAME):
                    yield name

    def _note_boundary(self, number: int, metadata: Mapping[str, Json]) -> None:
        preserved = self._json.mapping(metadata, Key.PRESERVED)
        self.boundaries.append(
            Boundary(
                line=number,
                trigger=self._json.text(metadata, Key.TRIGGER),
                discovered=self._json.strings(metadata, Key.DISCOVERED),
                preserved=self._json.strings(preserved, Key.UUIDS),
            )
        )


class SnapshotPolicy:
    """Выбор снимка prompt_snapshot, действующего для момента запроса (EU, kl, Ms).

    Клиент берёт последний снимок истории, а после первого ответа дописывает
    новый — с инструментами и префиксом, которые ушли в запрос. Поэтому для
    момента действует последний снимок, записанный до следующего момента в том
    же отрезке истории; если запрос ничего не дописал — последний до момента.
    """

    def __init__(self, facts: HistoryFacts) -> None:
        self._facts = facts

    def at(self, line: int) -> Snapshot:
        start = self._facts.segment_start(line)
        end = self._facts.window_end(line)
        chosen: Snapshot | None = None

        for snapshot in self._facts.snapshots:
            if snapshot.line < start:
                continue
            if snapshot.line >= end:
                break
            chosen = snapshot

        if chosen is None:
            raise RequestError(
                f"{self._facts.path}:{line}: building system and tools expects a prompt_snapshot attachment "
                f"between lines {start} and {end}, found none"
            )

        return chosen


class ToolOrder:
    """Порядок пула инструментов (wRt): встроенные по имени, затем MCP по имени.

    Клиент сортирует String.localeCompare; здесь воспроизведён корневой порядок
    ICU для ASCII-имён: знаки (_ раньше -), цифры, буквы без учёта регистра,
    при равенстве — строчные раньше прописных.
    """

    def sorted(self, names: Sequence[str]) -> list[str]:
        return sorted(names, key=self._key)

    def _key(self, name: str) -> tuple[bool, tuple[tuple[int, str], ...], str]:
        primary: list[tuple[int, str]] = []
        for char in name:
            primary.append(self._weight(char))

        return name.startswith(Wire.MCP_PREFIX), tuple(primary), name.swapcase()

    def _weight(self, char: str) -> tuple[int, str]:
        if char == "_":
            return 0, ""

        if char == "-":
            return 1, ""

        if char.isdigit():
            return 3, char

        if char.isalpha():
            return 4, char.lower()

        return 2, char


class ToolsBuilder:
    """Параметр tools на момент запроса.

    Инлайн-записи берёт из снимка, отложенные — из deferred_tools_record;
    какие отложенные едут в запрос и на каком месте, решает по вложениям
    deferred_tools_delta, границе сжатия и ссылкам tool_reference (uY, pUo).
    Заглушку и запись советника добавляет по внешним параметрам запуска.
    """

    def __init__(self, facts: HistoryFacts, launch: LaunchParams) -> None:
        self._facts = facts
        self._launch = launch
        self._order = ToolOrder()
        self._json = JsonView()

    def channel(self, line: int) -> bool:
        """Есть ли канал отложенных инструментов: клиент пишет дельту только при нём."""
        for delta in self._facts.deltas:
            if delta.line < line:
                return True

        return False

    def tool_search(self, line: int, snapshot: Snapshot) -> bool:
        if not self.channel(line):
            return False

        if self._search_absent(line):
            return False

        return Wire.TOOL_SEARCH in self._names(snapshot.tools)

    def build(self, line: int, snapshot: Snapshot, advisor_model: str, notes: list[str]) -> list[Json]:
        """Параметр tools: инлайн-записи снимка, отложенные записи пула и ушедшие из него."""
        inline: dict[str, Mapping[str, Json]] = {}
        for entry in snapshot.tools:
            inline[self._json.text(entry, Key.NAME)] = entry

        present: dict[str, Mapping[str, Json]] = {}
        departed: list[Mapping[str, Json]] = []

        if self.channel(line):
            recorded = self._recorded(line)
            pool = self._pool(line)
            for name in self._discovered(line, snapshot):
                if name in inline:
                    continue
                if name not in recorded:
                    self._require_absent(line, name, pool)
                    continue
                if name in pool:
                    present[name] = recorded[name]
                    continue
                departed.append(recorded[name])

        tools: list[Json] = []
        for name in self._order.sorted([*inline, *present]):
            if name in inline:
                tools.append(self._json.copy(dict(inline[name])))
                continue
            tools.append(self._json.copy(dict(present[name])))

        position = max(len(tools) - 1, 0)
        for offset, entry in enumerate(departed):
            tools.insert(position + offset, self._json.copy(dict(entry)))

        if self._stub_wanted(line, [*inline, *present]):
            tools.insert(max(len(tools) - 1, 0), self._placeholder())

        notes.append(
            f"tools: {len(inline)} inline from prompt_snapshot line {snapshot.line}, "
            f"{len(present)} deferred in the pool and {len(departed)} departed from deferred_tools_record"
        )

        if advisor_model:
            tools.append(self._advisor(advisor_model, snapshot, tools))

        return tools

    def _stub_wanted(self, line: int, names: Sequence[str]) -> bool:
        if not self._launch.deferred_stub_tool:
            return False

        if not self.channel(line):
            return False

        return Wire.PLACEHOLDER not in names

    def _placeholder(self) -> Json:
        return {
            Key.NAME: Wire.PLACEHOLDER,
            Key.DESCRIPTION: Wire.PLACEHOLDER_TEXT,
            Key.INPUT_SCHEMA: {Key.TYPE: Wire.OBJECT, Key.PROPERTIES: {}},
            Key.DEFER_LOADING: True,
        }

    def _advisor(self, model: str, snapshot: Snapshot, tools: Sequence[Json]) -> Json:
        """Запись советника; defer_loading — при advisorDeferred, бете tool-changes и неотложенной схеме."""
        entry: dict[str, Json] = {Key.TYPE: Wire.ADVISOR_TYPE, Key.NAME: Wire.ADVISOR, Key.MODEL: model}

        if not self._launch.advisor_deferred:
            return entry

        if not snapshot.tool_change_header:
            return entry

        for tool in tools:
            if self._listed(tool):
                entry[Key.DEFER_LOADING] = True
                break

        return entry

    def _listed(self, tool: Json) -> bool:
        if not isinstance(tool, dict):
            return False

        return tool.get(Key.DEFER_LOADING) is not True

    def _search_absent(self, line: int) -> bool:
        absent = False

        for delta in self._facts.deltas:
            if delta.line < line:
                absent = delta.search_absent

        return absent

    def _discovered(self, line: int, snapshot: Snapshot) -> list[str]:
        """uY @209064728: обнаруженные инструменты отрезка истории, в порядке появления.

        Имена из дельт считаются только при блоках tool_addition (nBe) и
        только у дельт, не объявленных «только именем» прежними запросами:
        свою запись deferred_tools_record запрос пишет уже после сборки tools.
        """
        start = self._facts.segment_start(line)
        found: dict[str, int] = {}

        for boundary in self._facts.boundaries:
            if boundary.line != start:
                continue
            for name in boundary.discovered:
                found.setdefault(name, boundary.line)

        name_only = self._name_only(line)
        for delta in self._facts.deltas:
            if not snapshot.tool_change_header or delta.uuid in name_only:
                continue
            if start <= delta.line < line:
                for name in delta.surfaced:
                    found.setdefault(name, delta.line)

        for reference in self._facts.references:
            if start <= reference.line < line:
                for name in reference.names:
                    found.setdefault(name, reference.line)

        return sorted(found, key=found.__getitem__)

    def _pool(self, line: int) -> set[str]:
        """Отложенные инструменты живого пула: свёртка дельт отрезка истории."""
        start = self._facts.segment_start(line)
        pool: set[str] = set()

        for delta in self._facts.deltas:
            if not start <= delta.line < line:
                continue
            pool.update(delta.added)
            pool.update(delta.surfaced)
            pool.difference_update(delta.removed)

        return pool

    def _name_only(self, line: int) -> set[str]:
        end = self._facts.request_floor(line)
        name_only: set[str] = set()

        for record in self._facts.deferred:
            if record.line < end:
                name_only.update(record.name_only)

        return name_only

    def _recorded(self, line: int) -> dict[str, Mapping[str, Json]]:
        end = self._facts.answer_line(line)
        recorded: dict[str, Mapping[str, Json]] = {}

        for record in self._facts.deferred:
            if record.line >= end:
                break
            for entry in record.entries:
                recorded[self._json.text(entry, Key.NAME)] = entry

        return recorded

    def _require_absent(self, line: int, name: str, pool: set[str]) -> None:
        if name not in pool:
            return

        raise RequestError(
            f"{self._facts.path}:{line}: deferred tool {name!r} is announced and present in the pool, "
            f"its definition is expected in a deferred_tools_record attachment, found none"
        )

    def _names(self, tools: Sequence[Mapping[str, Json]]) -> list[str]:
        names: list[str] = []
        for tool in tools:
            names.append(self._json.text(tool, Key.NAME))

        return names


class SystemBuilder:
    """Параметр system из снимка: префикс CLI и секции, разрезанные по границе (mFr, Ezo).

    Billing-блок не строится: в эталоне его нет, в дайджест он не входит.
    """

    def __init__(self, launch: LaunchParams) -> None:
        self._launch = launch
        self._stamp = CacheStamp()

    def build(self, snapshot: Snapshot, ttl: str, line: int, path: Path) -> list[Json]:
        prefix = snapshot.cli_prefix
        if not prefix:
            prefix = self._launch.cli_prefix

        if not prefix:
            raise RequestError(
                f"{path}:{line}: system expects cliPrefix in prompt_snapshot line {snapshot.line} "
                f"or cli_prefix in launch parameters, got neither"
            )

        sections = list(snapshot.system_prompt)
        if self._launch.system_context:
            sections.append(self._launch.system_context)

        if Wire.BOUNDARY not in sections:
            body = Wire.SECTION_JOIN.join(sections)
            return [self._block(prefix, self._stamp.mark(ttl, "")), self._block(body, self._stamp.mark(ttl, ""))]

        cut = sections.index(Wire.BOUNDARY)
        static = Wire.SECTION_JOIN.join(sections[:cut])
        dynamic = Wire.SECTION_JOIN.join(sections[cut + 1 :])
        blocks: list[Json] = [{Key.TYPE: Key.TEXT, Key.TEXT: prefix}]

        if static:
            blocks.append(self._block(static, self._stamp.mark(ttl, CacheKind.GLOBAL)))

        if dynamic:
            blocks.append(self._block(dynamic, self._stamp.mark(ttl, "")))

        return blocks

    def _block(self, text: str, mark: dict[str, Json]) -> Json:
        return {Key.TYPE: Key.TEXT, Key.TEXT: text, Key.CACHE_CONTROL: mark}


@dataclass(frozen=True)
class BetaFacts:
    """Признаки запроса, от которых зависят беты, выводимые из истории."""

    model: str
    snapshot: Snapshot
    advisor: bool
    tool_search: bool
    effort: bool
    ttl: str
    evict: bool
    fallback: bool
    threads: bool
    agentic: bool


class BetaPlan:
    """Список betas: беты запуска и модели плюс выводимые из истории, в порядке Beta.

    Бета afk-mode уходит только с запросами основного цикла и субагентов
    (isAgenticQuery, @209602696 и @209633655): у запроса суммаризации её нет.
    """

    def __init__(self, launch: LaunchParams) -> None:
        self._launch = launch

    def betas(self, facts: BetaFacts, model: ModelFacts) -> list[Json]:
        chosen = set(self._launch.environment_betas | model.betas)

        if Wire.HAIKU in facts.model:
            chosen.discard(Beta.CLAUDE_CODE)

        if not facts.threads:
            chosen.discard(Beta.MESSAGE_THREADS)

        if not facts.agentic:
            chosen.discard(Beta.AFK_MODE)

        derived: Sequence[tuple[Beta, bool]] = (
            (Beta.MID_CONVERSATION_SYSTEM, facts.snapshot.system_turns),
            (Beta.TOOL_CHANGES, facts.snapshot.tool_change_header),
            (Beta.INLINE_TOOLS, facts.snapshot.inline_tools),
            (Beta.ADVISOR_TOOL, facts.advisor),
            (Beta.ADVANCED_TOOL_USE, facts.tool_search),
            (Beta.SYSTEM_CLEAR_AT, facts.snapshot.kept_reminders),
            (Beta.EFFORT, facts.effort),
            (Beta.SERVER_SIDE_FALLBACK, facts.fallback),
            (Beta.FALLBACK_CREDIT, facts.fallback),
            (Beta.THINKING_DISPLAY_UPDATES, self._launch.thinking.display == Wire.DISPLAY_UPDATES),
            (Beta.EXTENDED_CACHE_TTL, facts.ttl == CacheKind.TTL_1H),
            (Beta.CACHE_EVICT, facts.evict),
        )

        for beta, wanted in derived:
            if wanted:
                chosen.add(beta)

        ordered: list[Json] = []
        for beta in Beta:
            if beta in chosen:
                ordered.append(str(beta))

        return ordered


class ParamFacts:
    """Параметры запроса из записей ассистента и вложения model на момент запроса."""

    def __init__(self, facts: HistoryFacts, launch: LaunchParams) -> None:
        self._facts = facts
        self._launch = launch

    def model(self, line: int, notes: list[str]) -> str:
        chosen = ""
        source = 0

        for note in self._facts.models:
            if note.line < line:
                chosen = note.names[0]
                source = note.line

        if chosen:
            notes.append(f"model: attachment model line {source}")
            return chosen

        if answer := self._answer(line):
            notes.append(f"model: assistant record line {answer.line} (written after the request)")
            return answer.model

        raise RequestError(
            f"{self._facts.path}:{line}: model expects an attachment of type model before the request "
            f"or an assistant record after it, found neither"
        )

    def effort(self, line: int, notes: list[str]) -> str:
        if (answer := self._answer(line)) and answer.effort:
            notes.append(f"output_config.effort: assistant record line {answer.line} (written after the request)")
            return answer.effort

        if self._launch.default_effort:
            notes.append("output_config.effort: launch parameters (the request has no answer in the journal yet)")
            return self._launch.default_effort

        if (previous := self._previous(line)) and previous.effort:
            notes.append(f"output_config.effort: assistant record line {previous.line} (the previous answer)")
            return previous.effort

        notes.append("output_config.effort: none (no assistant record carries effort, launch parameters give none)")
        return ""

    def turn_effort(self, line: int) -> str | None:
        """Усилие хода для PerTurnConfig: perTurnEffort последнего ответа до момента."""
        if (previous := self._previous(line)) and previous.per_turn_effort:
            return previous.per_turn_effort

        return None

    def advisor_model(self, line: int, notes: list[str]) -> str:
        if (answer := self._answer(line)) and answer.advisor_model:
            notes.append(f"advisor: assistant record line {answer.line} (written after the request)")
            return answer.advisor_model

        if (previous := self._previous(line)) and previous.advisor_model:
            notes.append(f"advisor: assistant record line {previous.line}")
            return previous.advisor_model

        return ""

    def _answer(self, line: int) -> AssistantFacts | None:
        """Первый ответ на запрос: запись ассистента в окне на строке запроса или после неё."""
        end = self._facts.window_end(line)

        for assistant in self._facts.assistants:
            if assistant.model == Synthetic.MODEL:
                continue
            if line <= assistant.line < end:
                return assistant

        return None

    def _previous(self, line: int) -> AssistantFacts | None:
        found: AssistantFacts | None = None

        for assistant in self._facts.assistants:
            if assistant.line < line and assistant.model != Synthetic.MODEL:
                found = assistant

        return found


class BodyAssembly:
    """Склейка тела из готовых частей и расстановка меток кэша в messages.

    Общая часть RequestBuilder и SummaryRequestBuilder: порядок ключей тела как
    у декодера эталона, context_management по thinking, output_config по усилию.
    """

    def __init__(self, launch: LaunchParams) -> None:
        self._launch = launch
        self._stamp = CacheStamp()
        self._json = JsonView()

    def messages(self, messages: Sequence[Mapping[str, Json]], marks: Sequence[MarkPath], ttl: str) -> list[Json]:
        copied: list[Json] = []
        for message in messages:
            copied.append(self._json.copy(dict(message)))

        for path in marks:
            target: Json = {Key.MESSAGES: copied}
            for step in path:
                target = self._step(target, step)
            if not isinstance(target, dict):
                raise RequestError(f"cache mark path {list(path)} expects a content block object, got {target!r}")
            target[Key.CACHE_CONTROL] = self._stamp.mark(ttl, "")

        return copied

    def body(
        self,
        model: str,
        system: list[Json],
        tools: list[Json],
        messages: list[Json],
        betas: list[Json],
        max_tokens: int,
        effort: str,
        extras: Mapping[str, Json],
        edits: Sequence[Mapping[str, Json]],
    ) -> dict[str, Json]:
        body: dict[str, Json] = {
            Key.MODEL: model,
            Key.SYSTEM: system,
            Key.TOOLS: tools,
            Key.MESSAGES: messages,
            Key.BETAS: betas,
            Key.MAX_TOKENS: max_tokens,
            Key.THINKING: self._thinking(),
        }

        if self._launch.thinking.active and Beta.CONTEXT_MANAGEMENT in betas:
            planned: list[Json] = [{Key.TYPE: Wire.CLEAR_THINKING, Key.KEEP: Wire.KEEP_ALL}]
            for edit in edits:
                planned.append(self._json.copy(dict(edit)))
            body[Key.CONTEXT_MANAGEMENT] = {Key.EDITS: planned}

        body.update(extras)

        if effort:
            body[Key.OUTPUT_CONFIG] = {Key.EFFORT: effort}

        return body

    def _thinking(self) -> Json:
        thinking: dict[str, Json] = {Key.TYPE: self._launch.thinking.kind}

        if self._launch.thinking.display:
            thinking[Key.DISPLAY] = self._launch.thinking.display

        return thinking

    def _step(self, target: Json, step: str | int) -> Json:
        if isinstance(target, dict) and isinstance(step, str):
            return target.get(step)

        if isinstance(target, list) and isinstance(step, int) and step < len(target):
            return target[step]

        return None


@dataclass(frozen=True)
class Composition:
    """Что отличает один запрос от другого при общей сборке тела."""

    line: int
    ttl: str
    messages: Sequence[Mapping[str, Json]]
    marks: Sequence[MarkPath]
    fallback: bool
    threads: bool
    evict: bool
    edits: Sequence[Mapping[str, Json]]
    agentic: bool


class RequestParts:
    """Общая сборка тела для основного запроса и запроса суммаризации.

    Держит строителей частей тела над одним журналом: SnapshotPolicy,
    ToolsBuilder, SystemBuilder, ParamFacts, BetaPlan и BodyAssembly. Создаётся
    RequestBuilder и SummaryRequestBuilder, которые готовят messages и передают
    сюда Composition.
    """

    def __init__(self, facts: HistoryFacts, launch: LaunchParams) -> None:
        self._facts = facts
        self._launch = launch
        self._snapshots = SnapshotPolicy(facts)
        self._tools = ToolsBuilder(facts, launch)
        self._system = SystemBuilder(launch)
        self._betas = BetaPlan(launch)
        self._assembly = BodyAssembly(launch)
        self._stamp = CacheStamp()
        self.params = ParamFacts(facts, launch)

    def compose(self, request: Composition, notes: list[str]) -> dict[str, Json]:
        line = request.line
        snapshot = self._snapshots.at(line)
        notes.append(f"system: prompt_snapshot line {snapshot.line}{self._lookahead(snapshot.line, line)}")

        model = self.params.model(line, notes)
        if model not in self._launch.models:
            raise RequestError(
                f"{self._facts.path}:{line}: launch parameters expect model facts (max_tokens, betas) "
                f"for {model!r}, known models are {sorted(self._launch.models)}"
            )

        catalog = self._launch.models[model]
        effort = self.params.effort(line, notes)
        advisor = self.params.advisor_model(line, notes)
        betas = self._betas.betas(
            BetaFacts(
                model=model,
                snapshot=snapshot,
                advisor=advisor != "",
                tool_search=self._tools.tool_search(line, snapshot),
                effort=effort != "",
                ttl=request.ttl,
                evict=request.evict,
                fallback=request.fallback,
                threads=request.threads,
                agentic=request.agentic,
            ),
            catalog,
        )

        extras: dict[str, Json] = {}
        if self._launch.safeguards:
            extras[Key.SAFEGUARDS] = list(self._launch.safeguards)

        if request.evict:
            mark = self._stamp.mark(request.ttl, "")
            mark[Key.EVICT] = True
            extras[Key.CACHE_CONTROL] = mark

        if request.fallback:
            extras[Key.FALLBACKS] = Wire.FALLBACKS_DEFAULT

        return self._assembly.body(
            model=model,
            system=self._system.build(snapshot, request.ttl, line, self._facts.path),
            tools=self._tools.build(line, snapshot, advisor, notes),
            messages=self._assembly.messages(request.messages, request.marks, request.ttl),
            betas=betas,
            max_tokens=catalog.max_tokens,
            effort=effort,
            extras=extras,
            edits=request.edits,
        )

    def marked(self, messages: Sequence[Mapping[str, Json]], marks: Sequence[MarkPath], ttl: str) -> list[Json]:
        """Сообщения провода с расставленными метками кэша."""
        return self._assembly.messages(messages, marks, ttl)

    def _lookahead(self, source: int, line: int) -> str:
        if source > line:
            return " (written after the request)"

        return ""


class ContextEdits(Protocol):
    """Правки context_management, которые планирует слой подрезки контекста.

    Реализует модель серверной очистки после простоя (compaction.IdleClearing);
    RequestBuilder спрашивает правку для строки запроса. Реализация сообщает
    о сбое ошибкой RequestError.
    """

    @abstractmethod
    def tool_clearing(self, line: int) -> Mapping[str, Json] | None: ...


class RequestBuilder:
    """Полное тело запроса основного цикла или субагента для строки журнала.

    Создаётся над SessionMemory: HistoryFacts даёт записи истории, Assembler —
    messages и пути меток, RequestParts собирает тело. Строка запроса — любая:
    история запроса — записи с меньшими номерами строк. Им пользуются вход
    командной строки и сверка.
    """

    def __init__(
        self, memory: SessionMemory, launch: LaunchParams, edits: ContextEdits, echo_api_block_order: bool
    ) -> None:
        self._memory = memory
        self._launch = launch
        self._edits = edits
        self.facts = HistoryFacts(memory.journal)
        self.facts.load()
        self.parts = RequestParts(self.facts, launch)
        self.assembler = Assembler(memory, echo_api_block_order)

    def build_all(self) -> list[BuiltRequest]:
        built: list[BuiltRequest] = []
        for moment in self._memory.moments():
            built.append(self.build(moment.line))

        return built

    def build(self, line: int) -> BuiltRequest:
        notes: list[str] = []
        clearing = self._edits.tool_clearing(line)
        edits: list[Mapping[str, Json]] = []
        threads = True
        if clearing is not None:
            edits.append(clearing)
            threads = self._threaded_before(line)
            notes.append(
                "context_management: clear_tool_uses edit after an idle gap; the request goes without a thread "
                f"(message-threads beta latched earlier in the process: {threads})"
            )

        assembled = self.messages(line, clearing is not None and self._launch.fork_cache_pin)

        ttl = self._launch.main_cache_ttl
        if self.facts.sidechain:
            ttl = self._launch.subagent_cache_ttl

        evict = False
        if self.facts.sidechain:
            evict = self._launch.subagent_cache_evict

        body = self.parts.compose(
            Composition(
                line=line,
                ttl=ttl,
                messages=assembled.messages,
                marks=assembled.marks,
                fallback=False,
                threads=threads,
                evict=evict,
                edits=tuple(edits),
                agentic=True,
            ),
            notes,
        )
        notes.extend(assembled.notes)
        return BuiltRequest(line=line, body=body, notes=tuple(notes))

    def messages(self, line: int, fork_pin: bool) -> Assembled:
        try:
            return self.assembler.assemble(self.assembler.moment_at(line, 0), fork_pin)
        except AssembleError as exc:
            raise RequestError(f"assembling messages of {self._memory.journal.path} before line {line} failed: {exc}") from exc

    def _threaded_before(self, line: int) -> bool:
        """Слал ли этот процесс раньше запрос с нитью: бета message-threads защёлкивается (SM @209634522)."""
        cut = self._memory.cut_for(line)

        for moment in self._memory.moments():
            if moment.line >= line:
                break
            if self._memory.cut_for(moment.line) != cut:
                continue
            if self._edits.tool_clearing(moment.line) is None:
                return True

        return False
