"""Загрузка журнала сессии при возобновлении (Claude Code 2.1.289, --resume).

Повторяет путь клиента от строк журнала до списка записей истории: раскладка
записей (processEntry в rbn), перешивка сохранённого хвоста после сжатия
(dAr), выбор листа (vo, Okn), сборка цепочки (Kut, pAr, mAr, uAr) и починка
для возобновления (zXt: BFe, pre, Enr, MXt, IXt). Сверх того — разметка
журнала на процессы и список записей в памяти процесса перед любой строкой
(SessionMemory): им пользуются сборка messages и модель сжатия.

Ошибки:
LoaderError — номер строки состояния вне журнала либо журнал нарушает
    предусловие операции; текст называет файл и причину. Ошибки чтения
    (JournalError) приходят упакованными в неё.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Iterator, Mapping, Sequence

from boba.prompt_assembly.records import (
    BlockType,
    Journal,
    JournalError,
    JsText,
    Json,
    Key,
    Raw,
    RecordType,
    Row,
    Subtype,
    Synthetic,
)



class LoaderError(Exception):
    """Журнал не загружен."""


class Phrase(StrEnum):
    """Тексты бинарника, по которым он узнаёт и создаёт синтетические записи."""

    NO_RESPONSE = "No response requested."
    NO_CONTENT = "(no content)"
    EMPTY_REMOVED = "[Empty text removed]"
    INTERRUPTED = "[Request interrupted by user]"
    INTERRUPTED_TOOL = "[Request interrupted by user for tool use]"
    TURN_ENDED = (
        "[Tool call did not complete: the turn was ended to deliver the message that follows. "
        "Nothing refused it; re-run it if still needed.]"
    )
    SESSION_ENDED = (
        "[Tool call interrupted: the session ended before this call's result was recorded, so its outcome "
        "is unknown. Check whether it took effect before relying on it or running it again.]"
    )
    INTERRUPTED_KIND = "interrupted"
    TASK_NOTIFICATION = "<task-notification>"
    HOOK_FEEDBACK = " hook feedback:\n"
    SYNTHETIC_UUID = "00000000-0000-4000-8000-{number:012d}"


class CommandTag(StrEnum):
    """Начала текста записей локальной команды (WR @201080375) и их роль."""

    RECORD = "<command-name>"
    STDOUT = "<local-command-stdout>"
    STDERR = "<local-command-stderr>"
    CAVEAT = "<local-command-caveat>"

    def role(self) -> str:
        if self is CommandTag.RECORD:
            return "record"

        if self is CommandTag.CAVEAT:
            return "caveat"

        return "output"


class TurnKind(StrEnum):
    """Классификация конца хода (Enr @210496260)."""

    NONE = "none"
    INTERRUPTED_PROMPT = "interrupted_prompt"
    INTERRUPTED_TURN = "interrupted_turn"
    ENDED_AT_MAX_TURNS = "ended_at_max_turns"


class LoadLimit(IntEnum):
    """Числовые константы загрузчика."""

    TIMESTAMP_FALLBACK_MS = 5000
    RETRACT_PREFIX = 24
    SKILL_NAMES = 4096
    SKILL_NAME_CHARS = 512
    STALE_API_ERROR_MS = 21600000
    LAST = 9007199254740991


class MetaRecord(StrEnum):
    """Записи метаданных «последняя побеждает» и поле, которое из них берётся."""

    CUSTOM_TITLE = "custom-title"
    AI_TITLE = "ai-title"
    TAG = "tag"
    RELOCATED = "relocated"
    AGENT_NAME = "agent-name"
    AGENT_COLOR = "agent-color"
    AGENT_SETTING = "agent-setting"
    MODE = "mode"
    PERMISSION_MODE = "permission-mode"

    def value_key(self) -> str:
        keys = {
            MetaRecord.CUSTOM_TITLE: "customTitle",
            MetaRecord.AI_TITLE: "aiTitle",
            MetaRecord.TAG: "tag",
            MetaRecord.RELOCATED: "relocatedCwd",
            MetaRecord.AGENT_NAME: "agentName",
            MetaRecord.AGENT_COLOR: "agentColor",
            MetaRecord.AGENT_SETTING: "agentSetting",
            MetaRecord.MODE: "mode",
            MetaRecord.PERMISSION_MODE: "permissionMode",
        }
        return keys[self]


@dataclass(frozen=True)
class LeafHints:
    """Состояние подсказок выбора листа после прохода по файлу (переменные rbn)."""

    last_written: str
    newest: str
    prompt_leaf: str
    explicit: bool
    strict: bool
    cleared: bool
    rewound: bool
    saw_marker: bool


@dataclass(frozen=True)
class SessionMetadata:
    """Метаданные сессии, которые возобновление возвращает вместе с историей."""

    session_id: str = ""
    custom_title: str = ""
    ai_title: str = ""
    tag: str = ""
    relocated_cwd: str = ""
    agent_name: str = ""
    agent_color: str = ""
    agent_setting: str = ""
    mode: str = ""
    permission_mode: str = ""
    atis_latch: str = ""
    summary: str = ""
    content_replacements: Sequence[Json] = ()
    file_snapshots: int = 0
    has_cost_state: bool = False
    ended_by_model: bool = False
    history_suppressed: bool = False
    rewind_anchor: str = ""
    cleared_to_empty: bool = False


@dataclass(frozen=True)
class TurnState:
    """Итог классификации конца хода и последняя пропущенная строка ошибки API."""

    kind: TurnKind
    skipped_api_error: Row | None = None


@dataclass(frozen=True)
class LoadResult:
    """Результат загрузки: история, дописываемые записи, метаданные и пояснения.

    found ложно, когда бинарник отвечает «No conversation found» (нет записей
    либо нет листа); при явном маркере «пусто» found истинно, а история пуста. appended — записи, которых нет в файле и которые процесс
    допишет в журнал перед первой новой записью; next_parent — uuid, к которому
    он подвесит первую собственную запись (новый промпт); reappended — типы
    записей метаданных, которые он повторит в хвосте файла.
    """

    found: bool
    history: Sequence[Row]
    appended: Sequence[Row]
    reappended: Sequence[str]
    metadata: SessionMetadata
    turn: TurnKind
    leaf: str
    next_parent: str
    skipped_lines: int
    notes: Sequence[str]


class RowKit:
    """Предикаты над строками цепочки; имена минифицированных функций — в комментариях."""

    def __init__(self) -> None:
        self._text = JsText()
        self._hook_prefixes = ("[structured-output-enforce]", "[projects-reply-gate]", "You ended the turn without calling SendUserMessage.")
        self._hook_events = ("Stop", "TeammateIdle", "TaskCreated", "TaskCompleted")
        self._retry_texts = frozenset(
            (
                "The previous response failed to produce a valid tool call. Please retry the tool call now.",
                "Your tool call was malformed and could not be parsed. Please retry.",
                "[Your previous response had no visible output. Please continue and produce a user-visible response.]",
                "The PermissionDenied hook indicated you may retry this tool call.",
            )
        )
        self._invisible_attachments = frozenset(
            ("thinking_drop", "credential_org", "prompt_snapshot", "prompt_render_point", "deferred_tools_record")
        )

    def tool_use_ids(self, row: Row) -> list[str]:
        """pye: id блоков tool_use строки assistant."""
        if row.kind != RecordType.ASSISTANT:
            return []

        return list(self._ids(row, BlockType.TOOL_USE, Key.ID))

    def tool_result_ids(self, row: Row) -> list[str]:
        """RN: tool_use_id блоков tool_result строки user."""
        if row.kind != RecordType.USER:
            return []

        return list(self._ids(row, BlockType.TOOL_RESULT, Key.TOOL_USE_ID))

    def has_block(self, row: Row, kind: str) -> bool:
        """AP: в содержимом-массиве есть блок данного типа."""
        for block in row.blocks:
            if block.get(Key.TYPE) == kind:
                return True

        return False

    def boundary(self, row: Row) -> bool:
        """ai: граница сжатия."""
        return row.kind == RecordType.SYSTEM and row.subtype == Subtype.COMPACT_BOUNDARY

    def delivery(self, row: Row) -> bool:
        """DL: вложение, system не-граница либо meta-user без tool_result."""
        if row.kind == RecordType.ATTACHMENT:
            return True

        if row.kind == RecordType.SYSTEM:
            return row.subtype != Subtype.COMPACT_BOUNDARY

        if row.kind != RecordType.USER:
            return False

        if not row.flag(Key.META):
            return False

        return not self.has_block(row, BlockType.TOOL_RESULT)

    def continues(self, row: Row, message_id: str | None) -> bool:
        """kye: строка продолжает ход ответа message_id."""
        if row.kind == RecordType.ASSISTANT:
            return message_id is not None and row.message_id == message_id

        if row.kind == RecordType.USER:
            if row.flag(Key.META):
                return True
            return self.has_block(row, BlockType.TOOL_RESULT)

        if row.kind == RecordType.ATTACHMENT:
            return True

        if row.kind == RecordType.SYSTEM:
            return row.subtype != Subtype.COMPACT_BOUNDARY

        return False

    def virtual(self, row: Row) -> bool:
        """WO."""
        if row.kind not in (RecordType.USER, RecordType.ASSISTANT):
            return False

        return row.flag(Key.VIRTUAL)

    def synthetic_error(self, row: Row) -> bool:
        """zoe: синтетическая строка ошибки API."""
        if row.kind != RecordType.ASSISTANT:
            return False

        if not row.flag(Key.API_ERROR):
            return False

        return row.message.get(Key.MODEL) == Synthetic.MODEL

    def local_command(self, row: Row) -> bool:
        """w8."""
        return row.kind == RecordType.SYSTEM and row.subtype == Subtype.LOCAL_COMMAND

    def invisible(self, row: Row) -> bool:
        """ny: строка, которую модель не видит."""
        if row.kind == RecordType.PROGRESS:
            return True

        if row.kind == RecordType.SYSTEM:
            return not self.local_command(row)

        if self.virtual(row):
            return True

        if self.synthetic_error(row):
            return True

        if row.kind != RecordType.ATTACHMENT:
            return False

        return row.attachment_kind in self._invisible_attachments

    def single_text(self, content: Json) -> str | None:
        """rI: строка либо текст единственного text-блока."""
        if isinstance(content, str):
            return content

        if not isinstance(content, list) or len(content) != 1:
            return None

        block = content[0]
        if not isinstance(block, dict) or block.get(Key.TYPE) != BlockType.TEXT:
            return None

        text = block.get(Key.TEXT)
        if isinstance(text, str):
            return text

        return None

    def first_text(self, content: Json) -> str | None:
        """D @200875714: строка либо текст первого блока, если он text."""
        if isinstance(content, str):
            return content

        if not isinstance(content, list) or not content:
            return None

        block = content[0]
        if not isinstance(block, dict) or block.get(Key.TYPE) != BlockType.TEXT:
            return None

        text = block.get(Key.TEXT)
        if isinstance(text, str):
            return text

        return None

    def hook_meta(self, row: Row) -> bool:
        """qTn: meta-user с текстом хука либо служебной просьбой повторить."""
        if row.kind != RecordType.USER or not row.flag(Key.META):
            return False

        text = self.first_text(row.content)
        if text is None:
            return False

        if text.startswith(self._hook_prefixes):
            return True

        if text in self._retry_texts:
            return True

        for event in self._hook_events:
            if text.startswith(f"{event}{Phrase.HOOK_FEEDBACK}"):
                return True

        return False

    def companion(self, row: Row) -> bool:
        """cpe: user-строка, сопровождающая ход (без ветки D_e, она под переменной окружения)."""
        if row.kind != RecordType.USER:
            return False

        if row.flag("turnCompanion"):
            return True

        if isinstance(row.raw.get("sourceToolUseID"), str):
            return True

        return self.hook_meta(row)

    def task_notification(self, row: Row) -> bool:
        """y1e."""
        origin = row.raw.get("origin")
        if not isinstance(origin, dict) or origin.get("kind") != "task-notification":
            return False

        text = self.single_text(row.content)
        if text is None:
            return False

        return text.startswith(Phrase.TASK_NOTIFICATION)

    def queued_notification(self, row: Row) -> bool:
        """M6."""
        if row.kind != RecordType.USER or not row.flag("queueTranscriptOnly"):
            return False

        return self.task_notification(row)

    def thinking_block(self, block: Raw) -> bool:
        """Vde."""
        return block.get(Key.TYPE) in (BlockType.THINKING, BlockType.REDACTED_THINKING)

    def substantive(self, block: Raw) -> bool:
        """jCr: блок не thinking и не пустой текст."""
        if self.thinking_block(block):
            return False

        if block.get(Key.TYPE) != BlockType.TEXT:
            return True

        text = block.get(Key.TEXT)
        if not isinstance(text, str):
            text = ""

        return self._text.trim(text) not in ("", Phrase.NO_CONTENT, Phrase.EMPTY_REMOVED)

    def blank(self, blocks: Sequence[Raw]) -> bool:
        """sye: после ведущих thinking идут только пустые text-блоки, и хотя бы один есть."""
        seen_text = False

        for block in blocks:
            if not seen_text and self.thinking_block(block):
                continue
            if block.get(Key.TYPE) != BlockType.TEXT:
                return False
            text = block.get(Key.TEXT)
            if isinstance(text, str) and self._text.trim(text) not in ("", Phrase.NO_CONTENT, Phrase.EMPTY_REMOVED):
                return False
            seen_text = True

        return seen_text

    def has_plain_block(self, blocks: Sequence[Raw]) -> bool:
        """Среди блоков есть хотя бы один не thinking."""
        for block in blocks:
            if not self.thinking_block(block):
                return True

        return False

    def has_substantive(self, blocks: Sequence[Raw]) -> bool:
        for block in blocks:
            if self.substantive(block):
                return True

        return False

    def all_in(self, names: Sequence[str], pool: set[str]) -> bool:
        """Список не пуст и целиком входит в набор."""
        if not names:
            return False

        for name in names:
            if name not in pool:
                return False

        return True

    def uuids(self, rows: Sequence[Row]) -> set[str]:
        found: set[str] = set()
        for row in rows:
            found.add(row.uuid)

        return found

    def _ids(self, row: Row, kind: str, key: str) -> Iterator[str]:
        for block in row.blocks:
            if block.get(Key.TYPE) != kind:
                continue
            value = block.get(key)
            if isinstance(value, str):
                yield value


class RowRepair:
    """Починка строк при разборе (bXt @210479270, wXt @210480246).

    Создаётся TranscriptIndex'ом; admit решает судьбу строки user/assistant,
    finish перешивает детей отброшенных строк к ближайшему уцелевшему предку.
    Починка полей system-строк (CXt/Ntr) не моделируется.
    """

    def __init__(self) -> None:
        self._text = JsText()
        self._dropped: dict[str, str | None] = {}

    def admit(self, row: Row, known: Mapping[str, Row]) -> Row | None:
        if row.kind not in (RecordType.USER, RecordType.ASSISTANT):
            return row

        repaired = self.message(row)
        if repaired is not None:
            return repaired

        if row.uuid not in known:
            self._dropped[row.uuid] = row.parent

        return None

    def message(self, row: Row) -> Row | None:
        """wXt: None — строку отбросить, иначе строка с пригодным содержимым."""
        if not isinstance(row.raw.get(Key.MESSAGE), dict):
            return None

        content = row.content
        if isinstance(content, str):
            if row.kind != RecordType.ASSISTANT:
                return row
            if self._text.trim(content) == "":
                return None
            return row.with_content([{Key.TYPE.value: BlockType.TEXT.value, Key.TEXT.value: content}])

        if not isinstance(content, list):
            return None

        kept: list[Json] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get(Key.TYPE), str):
                kept.append(block)

        if len(kept) == len(content):
            return row

        if not kept:
            return None

        return row.with_content(kept)

    def finish(self, messages: dict[str, Row]) -> None:
        if not self._dropped:
            return

        for uuid, row in list(messages.items()):
            parent = row.parent
            if parent is None or parent in messages or parent not in self._dropped:
                continue
            messages[uuid] = row.with_parent(self._survivor(parent, messages))

    def _survivor(self, start: str, messages: Mapping[str, Row]) -> str | None:
        seen: set[str] = set()
        cursor: str | None = start

        while cursor is not None and cursor not in messages and cursor in self._dropped:
            if cursor in seen:
                return None
            seen.add(cursor)
            cursor = self._dropped[cursor]

        return cursor


class PreservedRelink:
    """Перешивка сохранённого хвоста после сжатия (dAr @211872778, cAr @211874082).

    Работает над словарём сообщений TranscriptIndex'а: ставит сохранённые
    записи после якоря (сводки) и удаляет всё, что лежит до последней границы.
    Возвращает uuid хвоста либо None, когда перешивки не было.
    """

    def __init__(self, kit: RowKit, notes: list[str]) -> None:
        self._kit = kit
        self._notes = notes

    def apply(self, messages: dict[str, Row]) -> str | None:
        order: dict[str, int] = {}
        last_boundary = -1
        preserved_at = -1
        metadata: Raw | None = None

        for position, row in enumerate(messages.values()):
            order[row.uuid] = position
            if not self._kit.boundary(row):
                continue
            last_boundary = position
            candidate = row.raw.get(Key.COMPACT_METADATA)
            if not isinstance(candidate, dict):
                continue
            if candidate.get("preservedMessages") or candidate.get("preservedSegment"):
                metadata = candidate
                preserved_at = position

        if metadata is None:
            return None

        uuids: list[str] = []
        anchor = ""
        if preserved_at == last_boundary:
            resolved = self._resolve(metadata, messages)
            if resolved is None:
                return None
            anchor, uuids = resolved

        for uuid in uuids:
            if uuid not in messages:
                self._notes.append(f"relink aborted: preserved uuid {uuid} is not in the transcript, nothing relinked or removed")
                return None

        kept = set(uuids)
        if uuids:
            self._chain_after(anchor, uuids, messages)

        doomed: list[str] = []
        for uuid in messages:
            if order[uuid] < last_boundary and uuid not in kept:
                doomed.append(uuid)

        for uuid in doomed:
            del messages[uuid]

        if uuids and doomed:
            self._adopt(set(doomed), uuids[-1], messages)

        if not uuids:
            return None

        return uuids[-1]

    def _resolve(self, metadata: Raw, messages: Mapping[str, Row]) -> tuple[str, list[str]] | None:
        listed = metadata.get("preservedMessages")
        if isinstance(listed, dict):
            names: list[str] = []
            raw_names = listed.get("uuids")
            if isinstance(raw_names, list):
                for name in raw_names:
                    if isinstance(name, str):
                        names.append(name)
            return self._name(listed.get("anchorUuid")), names

        segment = metadata.get("preservedSegment")
        if not isinstance(segment, dict):
            return None

        head = self._name(segment.get("headUuid"))
        seen: set[str] = set()
        walked: list[str] = []
        cursor = messages.get(self._name(segment.get("tailUuid")))

        while cursor is not None and cursor.uuid not in seen:
            seen.add(cursor.uuid)
            walked.append(cursor.uuid)
            if cursor.uuid == head:
                walked.reverse()
                return self._name(segment.get("anchorUuid")), walked
            parent = cursor.parent
            cursor = None
            if parent is not None:
                cursor = messages.get(parent)

        self._notes.append("relink aborted: walking preservedSegment from tail to head broke off")
        return None

    def _chain_after(self, anchor: str, uuids: Sequence[str], messages: dict[str, Row]) -> None:
        tail = uuids[-1]
        parent = anchor

        for uuid in uuids:
            messages[uuid] = messages[uuid].with_parent(parent)
            parent = uuid

        for uuid, row in list(messages.items()):
            if row.parent == anchor and uuid != uuids[0]:
                messages[uuid] = row.with_parent(tail)

    def _adopt(self, doomed: set[str], tail: str, messages: dict[str, Row]) -> None:
        for uuid, row in list(messages.items()):
            if row.kind not in (RecordType.USER, RecordType.ASSISTANT):
                continue
            if row.parent is not None and row.parent in doomed:
                messages[uuid] = row.with_parent(tail)

    def _name(self, value: Json) -> str:
        if isinstance(value, str):
            return value

        return ""


class LeafSelector:
    """Выбор листьев графа (vo @211938215) и проверка продолжения ветки (Okn @211930929)."""

    def __init__(self, kit: RowKit, notes: list[str]) -> None:
        self._kit = kit
        self._notes = notes

    def select(self, messages: Mapping[str, Row], hints: LeafHints, relinked: str | None) -> list[str]:
        if hints.cleared:
            return []

        marker = hints.prompt_leaf
        explicit_ok = False
        if hints.explicit and marker in messages:
            explicit_ok = not messages[marker].sidechain

        fallback = hints.last_written
        if not hints.saw_marker and fallback and hints.newest and hints.newest != fallback:
            fallback = self._newest_descendant(messages, fallback)

        if relinked is None or explicit_ok:
            leaf = self._hinted(messages, hints, relinked, fallback)
            if leaf is not None:
                return [leaf]

        leaves = self._terminals(messages)
        if len(leaves) <= 1:
            return leaves

        pick = hints.last_written
        if marker in leaves and self._marker_wins(messages, hints):
            pick = marker

        if not pick or pick not in messages:
            return leaves

        top = self._conversational(messages, pick)
        if top is None:
            return leaves

        return [top]

    def continues(self, messages: Mapping[str, Row], start: str, marker: str, direct: bool, through_batch: bool) -> bool:
        """Okn: ветка от start вверх проходит через marker либо через ответ, которому marker принадлежит."""
        owner: Row | None = None
        results: list[str] = []
        by_attachment = False

        if through_batch:
            owner, results, by_attachment = self._batch_owner(messages, marker)

        owner_ids: list[str] = []
        response: str | None = None
        siblings: set[str] = set()
        if owner is not None:
            owner_ids = self._kit.tool_use_ids(owner)
            if owner.kind == RecordType.ASSISTANT and owner_ids:
                response = owner.message_id
        if owner is not None and response is not None:
            for sibling in self._response_rows_above(messages, owner, response):
                siblings.add(sibling.uuid)

        visited: set[str] = set()
        previous: Row | None = None
        in_response = False
        cursor: str | None = start

        while cursor and cursor not in visited:
            if direct and cursor == marker:
                return True
            visited.add(cursor)
            row = messages.get(cursor)

            if owner is not None and response is not None and row is not None:
                if in_response and row.uuid == owner.uuid:
                    return True
                same_response = (
                    row.kind == RecordType.ASSISTANT
                    and row.uuid != owner.uuid
                    and row.message_id == response
                    and self._kit.has_block(row, BlockType.TOOL_USE)
                )
                if same_response:
                    if row.uuid in siblings:
                        return True
                    in_response = True
                if not same_response and not self._kit.continues(row, response):
                    in_response = False

            if owner is not None and row is not None and row.uuid == owner.uuid and previous is not None:
                if self._sibling_result(previous, owner_ids, results, by_attachment):
                    return True

            previous = row
            cursor = None
            if row is not None:
                cursor = row.parent

        return False

    def _hinted(self, messages: Mapping[str, Row], hints: LeafHints, relinked: str | None, fallback: str) -> str | None:
        leaf = ""
        if hints.prompt_leaf in messages:
            leaf = hints.prompt_leaf

        last = hints.last_written
        if leaf and not hints.explicit and last in messages and last != leaf:
            if self.continues(messages, last, leaf, direct=True, through_batch=not hints.strict):
                leaf = last

        if relinked is None and not leaf:
            leaf = fallback

        if not leaf or leaf not in messages:
            return None

        return self._conversational(messages, leaf)

    def _conversational(self, messages: Mapping[str, Row], start: str) -> str | None:
        seen: set[str] = set()
        row = messages.get(start)

        while row is not None:
            if row.uuid in seen:
                self._notes.append(f"parentUuid cycle while looking for the leaf above {start}")
                return None
            seen.add(row.uuid)
            if row.kind in (RecordType.USER, RecordType.ASSISTANT):
                return row.uuid
            parent = row.parent
            row = None
            if parent is not None:
                row = messages.get(parent)

        return None

    def _terminals(self, messages: Mapping[str, Row]) -> list[str]:
        has_child: set[str] = set()
        has_talking_child: set[str] = set()

        for row in messages.values():
            parent = row.parent
            if parent is None:
                continue
            has_child.add(parent)
            if row.kind in (RecordType.USER, RecordType.ASSISTANT):
                has_talking_child.add(parent)

        leaves: list[str] = []
        for row in messages.values():
            if row.uuid in has_child:
                continue
            top = self._conversational(messages, row.uuid)
            if top is None or top in has_talking_child or top in leaves:
                continue
            leaves.append(top)

        return leaves

    def _marker_wins(self, messages: Mapping[str, Row], hints: LeafHints) -> bool:
        if hints.explicit:
            return True

        if not hints.last_written:
            return True

        return not self.continues(messages, hints.last_written, hints.prompt_leaf, direct=False, through_batch=not hints.strict)

    def _newest_descendant(self, messages: Mapping[str, Row], root: str) -> str:
        """yr: самая поздняя по timestamp запись главной цепочки среди потомков root."""
        below: dict[str, bool] = {root: True}
        best = root
        best_stamp = ""

        for row in messages.values():
            if row.sidechain or row.uuid == root or not row.timestamp:
                continue
            if row.timestamp < best_stamp:
                continue
            if self._descends(messages, row.uuid, below):
                best = row.uuid
                best_stamp = row.timestamp

        return best

    def _descends(self, messages: Mapping[str, Row], start: str, below: dict[str, bool]) -> bool:
        path: list[str] = []
        verdict = False
        cursor: str | None = start

        while cursor:
            known = below.get(cursor)
            if known is not None:
                verdict = known
                break
            below[cursor] = False
            path.append(cursor)
            row = messages.get(cursor)
            cursor = None
            if row is not None:
                cursor = row.parent

        for uuid in path:
            below[uuid] = verdict

        return verdict

    def _batch_owner(self, messages: Mapping[str, Row], marker: str) -> tuple[Row | None, list[str], bool]:
        seen: set[str] = set()
        lowest: Row | None = None
        row = messages.get(marker)

        while row is not None and self._kit.delivery(row) and row.uuid not in seen:
            seen.add(row.uuid)
            lowest = row
            row = self._parent(messages, row)

        if row is not None and row.kind == RecordType.USER and self._kit.has_block(row, BlockType.TOOL_RESULT):
            return self._parent(messages, row), self._kit.tool_result_ids(row), False

        if lowest is None:
            return row, [], False

        call = lowest.attachment.get("toolUseID")
        if row is None or row.kind != RecordType.ASSISTANT or row.message_id is None or not isinstance(call, str):
            return None, [], False

        for candidate in (row, *self._response_rows_above(messages, row, row.message_id)):
            if call in self._kit.tool_use_ids(candidate):
                return candidate, [call], True

        return None, [], False

    def _response_rows_above(self, messages: Mapping[str, Row], row: Row, response: str) -> list[Row]:
        """Dkn: предки строки, пока они продолжают ход ответа."""
        result: list[Row] = []
        seen = {row.uuid}
        cursor = self._parent(messages, row)

        while cursor is not None and cursor.uuid not in seen and self._kit.continues(cursor, response):
            seen.add(cursor.uuid)
            result.append(cursor)
            cursor = self._parent(messages, cursor)

        return result

    def _sibling_result(self, below: Row, owner_ids: Sequence[str], results: Sequence[str], by_attachment: bool) -> bool:
        shared = False
        for result in results:
            if result in owner_ids:
                shared = True

        if not shared:
            return False

        for result in self._kit.tool_result_ids(below):
            if result not in owner_ids:
                continue
            if by_attachment and len(owner_ids) > 1:
                return True
            if not by_attachment and result not in results:
                return True

        return False

    def _parent(self, messages: Mapping[str, Row], row: Row) -> Row | None:
        parent = row.parent
        if parent is None:
            return None

        return messages.get(parent)


class TranscriptIndex:
    """Раскладка записей файла (processEntry в rbn @211932344) и итог finish().

    Создаётся SessionLoader'ом; копит сообщения по uuid в порядке файла,
    подсказки листа и метаданные, в finish() зовёт RowRepair, PreservedRelink
    и LeafSelector.
    """

    def __init__(self, kit: RowKit, notes: list[str]) -> None:
        self._kit = kit
        self._notes = notes
        self._repair = RowRepair()
        self._relink = PreservedRelink(kit, notes)
        self._selector = LeafSelector(kit, notes)
        self.messages: dict[str, Row] = {}
        self._bridge: dict[str, str | None] = {}
        self._last_written = ""
        self._newest = ""
        self._newest_stamp = ""
        self._prompt_leaf = ""
        self._explicit = False
        self._strict = False
        self._cleared = False
        self._rewound = False
        self._saw_marker = False
        self._summaries: dict[str, str] = {}
        self._meta: dict[tuple[str, str], str] = {}
        self._atis: dict[str, str] = {}
        self._replacements: dict[str, list[Json]] = {}
        self._snapshots: set[str] = set()
        self._cost: set[str] = set()
        self._ended: set[str] = set()
        self._suppressed: set[str] = set()

    def take(self, line: int, raw: Raw) -> None:
        kind = raw.get(Key.TYPE)
        uuid = raw.get(Key.UUID)

        if kind == RecordType.PROGRESS and isinstance(uuid, str):
            self._take_progress(uuid, raw)
            return

        if isinstance(kind, str) and kind in tuple(RecordType) and RecordType(kind).in_transcript():
            self._take_row(Row(line=line, raw=raw))
            return

        if kind == RecordType.LAST_PROMPT:
            self._take_marker(raw)
            return

        self._take_metadata(str(kind), raw)

    def finish(self) -> tuple[list[str], LeafHints]:
        self._repair.finish(self.messages)
        relinked = self._relink.apply(self.messages)
        hints = LeafHints(
            last_written=self._last_written,
            newest=self._newest,
            prompt_leaf=self._prompt_leaf,
            explicit=self._explicit,
            strict=self._strict,
            cleared=self._cleared,
            rewound=self._rewound,
            saw_marker=self._saw_marker,
        )
        return self._selector.select(self.messages, hints, relinked), hints

    def metadata(self, session: str, leaf: str, chain: Sequence[Row], hints: LeafHints) -> SessionMetadata:
        snapshots = 0
        for row in chain:
            if row.uuid in self._snapshots:
                snapshots += 1

        anchor = ""
        if hints.rewound:
            anchor = hints.prompt_leaf

        return SessionMetadata(
            session_id=session,
            custom_title=self._meta.get((MetaRecord.CUSTOM_TITLE, session), ""),
            ai_title=self._meta.get((MetaRecord.AI_TITLE, session), ""),
            tag=self._meta.get((MetaRecord.TAG, session), ""),
            relocated_cwd=self._meta.get((MetaRecord.RELOCATED, session), ""),
            agent_name=self._meta.get((MetaRecord.AGENT_NAME, session), ""),
            agent_color=self._meta.get((MetaRecord.AGENT_COLOR, session), ""),
            agent_setting=self._meta.get((MetaRecord.AGENT_SETTING, session), ""),
            mode=self._meta.get((MetaRecord.MODE, session), ""),
            permission_mode=self._meta.get((MetaRecord.PERMISSION_MODE, session), ""),
            atis_latch=self._atis.get(session, ""),
            summary=self._summaries.get(leaf, ""),
            content_replacements=tuple(self._replacements.get(session, [])),
            file_snapshots=snapshots,
            has_cost_state=session in self._cost,
            ended_by_model=session in self._ended,
            history_suppressed=session in self._suppressed,
            rewind_anchor=anchor,
            cleared_to_empty=hints.cleared,
        )

    def _take_progress(self, uuid: str, raw: Raw) -> None:
        parent = raw.get(Key.PARENT)
        if not isinstance(parent, str) or not parent:
            self._bridge[uuid] = None
            return

        if parent in self._bridge:
            self._bridge[uuid] = self._bridge[parent]
            return

        self._bridge[uuid] = parent

    def _take_row(self, row: Row) -> None:
        parent = row.parent
        if parent is not None and parent in self._bridge:
            row = row.with_parent(self._bridge[parent])

        admitted = self._repair.admit(row, self.messages)
        if admitted is None:
            self._notes.append(f"line {row.line}: unreadable {row.kind} row dropped at load")
            return

        if admitted.uuid in self.messages:
            self._notes.append(f"line {row.line}: duplicate uuid {admitted.uuid} replaces the earlier row in place")

        self.messages[admitted.uuid] = admitted

        briefing = admitted.kind == RecordType.ATTACHMENT and admitted.attachment_kind == Subtype.FORK_BRIEFING
        if not admitted.sidechain and not briefing:
            self._last_written = admitted.uuid
            if admitted.timestamp and admitted.timestamp > self._newest_stamp:
                self._newest = admitted.uuid
                self._newest_stamp = admitted.timestamp
            self._explicit = False
            self._cleared = False
            self._rewound = False

        if self._kit.boundary(admitted):
            self._prompt_leaf = ""
            self._explicit = False
            self._strict = False

    def _take_marker(self, raw: Raw) -> None:
        if Key.LEAF in raw:
            self._saw_marker = True

        leaf = raw.get(Key.LEAF)
        explicit = raw.get(Key.EXPLICIT) is True

        if isinstance(leaf, str) and leaf:
            same = leaf == self._prompt_leaf
            self._explicit = explicit or (self._explicit and same)
            self._strict = explicit or (self._strict and same)
            self._rewound = raw.get(Key.REWOUND) is True or (self._rewound and same)
            self._prompt_leaf = leaf
            self._cleared = False
            return

        if Key.LEAF in raw and leaf is None and explicit:
            self._cleared = True
            self._prompt_leaf = ""
            self._explicit = False
            self._strict = False
            self._rewound = False

    def _take_metadata(self, kind: str, raw: Raw) -> None:
        if kind == RecordType.SUMMARY:
            leaf = raw.get(Key.LEAF)
            summary = raw.get("summary")
            if isinstance(leaf, str) and leaf and isinstance(summary, str):
                self._summaries[leaf] = summary
            return

        if kind == RecordType.FILE_SNAPSHOT:
            message = raw.get("messageId")
            if isinstance(message, str):
                self._snapshots.add(message)
            return

        session = raw.get(Key.SESSION)
        if not isinstance(session, str) or not session:
            return

        if kind == RecordType.CONTENT_REPLACEMENT:
            replacements = raw.get("replacements")
            if isinstance(replacements, list) and not raw.get(Key.AGENT):
                self._replacements.setdefault(session, []).extend(replacements)
            return

        if kind == RecordType.ATIS_LATCH:
            atis = raw.get("atis")
            if isinstance(atis, str) and atis.isascii() and atis.isprintable() and " " not in atis:
                self._atis[session] = atis
            return

        if kind == RecordType.COST_STATE:
            self._cost.add(session)
            return

        if kind == RecordType.ENDED_BY_MODEL:
            self._ended.add(session)
            return

        if kind == RecordType.HISTORY_SUPPRESSION:
            self._suppressed.add(session)
            return

        if kind not in tuple(MetaRecord):
            return

        record = MetaRecord(kind)
        value = raw.get(record.value_key())
        if isinstance(value, str):
            self._meta[(record, session)] = value


@dataclass(frozen=True)
class _Recovery:
    """Отрезок цепочки одного ответа и строки, которые mAr в него возвращает."""

    start: int
    end: int
    recovered: Sequence[Row]
    anchored: Mapping[str, Sequence[Row]]


class ChainBuilder:
    """Сборка цепочки от листа к корню (Kut @211874772) с добором соседних веток.

    pAr подставляет предка по времени при разрыве, mAr @211878079 возвращает
    строки параллельных вызовов одного ответа, uAr дописывает хвост вложений
    после листа. Зовётся SessionLoader'ом после выбора листа.
    """

    def __init__(self, kit: RowKit, notes: list[str]) -> None:
        self._kit = kit
        self._notes = notes
        self._text = JsText()

    def pick_leaf(self, messages: Mapping[str, Row], leaves: Sequence[str], cleared: bool) -> Row | None:
        """Ass @211947161: самая поздняя по времени запись среди листьев либо, без листьев, во всём файле."""
        wanted = set(leaves)
        best = self._latest(messages, wanted)
        if best is not None:
            return best

        if wanted or cleared:
            return None

        return self._latest(messages, None)

    def build(self, messages: Mapping[str, Row], leaf: Row) -> list[Row]:
        chain: list[Row] = []
        seen: set[str] = set()
        cursor: Row | None = leaf

        while cursor is not None:
            if cursor.uuid in seen:
                self._notes.append(f"cycle in parentUuid chain at {cursor.uuid}, partial transcript returned")
                break
            seen.add(cursor.uuid)
            chain.append(cursor)
            parent = cursor.parent
            if parent is None:
                break
            above = messages.get(parent)
            if above is None or above.uuid in seen:
                above = self._nearest_earlier(messages, cursor, seen)
                self._note_fallback(cursor, parent, above)
            cursor = above

        chain.reverse()
        chain = self._recover_parallel(messages, chain, seen)
        self._append_tail(messages, leaf, chain, seen)
        return chain

    def _latest(self, messages: Mapping[str, Row], wanted: set[str] | None) -> Row | None:
        best: Row | None = None
        best_stamp = float("-inf")

        for row in messages.values():
            if row.sidechain:
                continue
            if wanted is not None and (row.uuid not in wanted or row.kind not in (RecordType.USER, RecordType.ASSISTANT)):
                continue
            stamp = self._text.epoch_ms(row.timestamp)
            if stamp is not None and stamp > best_stamp:
                best = row
                best_stamp = stamp

        return best

    def _note_fallback(self, row: Row, parent: str, above: Row | None) -> None:
        if above is None:
            self._notes.append(f"line {row.line}: parent {parent} is missing or already on the chain and no row within 5000 ms precedes it, chain ends here")
            return

        self._notes.append(f"line {row.line}: parent {parent} is missing or already on the chain, nearest earlier row at line {above.line} taken by timestamp")

    def _nearest_earlier(self, messages: Mapping[str, Row], row: Row, seen: set[str]) -> Row | None:
        """pAr @211876837."""
        moment = self._text.epoch_ms(row.timestamp)
        if moment is None:
            return None

        best: Row | None = None
        best_gap = float("inf")

        for candidate in messages.values():
            if candidate.uuid in seen:
                continue
            if candidate.raw.get(Key.SIDECHAIN) != row.raw.get(Key.SIDECHAIN):
                continue
            stamp = self._text.epoch_ms(candidate.timestamp)
            if stamp is None:
                continue
            gap = moment - stamp
            if 0 <= gap <= LoadLimit.TIMESTAMP_FALLBACK_MS and gap < best_gap:
                best = candidate
                best_gap = gap

        return best

    def _append_tail(self, messages: Mapping[str, Row], leaf: Row, chain: list[Row], seen: set[str]) -> None:
        """uAr в режиме I8o: обход в глубину по детям, не являющимся user/assistant."""
        children: dict[str, list[Row]] = {}
        for row in messages.values():
            parent = row.parent
            if parent is None or row.kind in (RecordType.USER, RecordType.ASSISTANT):
                continue
            children.setdefault(parent, []).append(row)

        stack = [leaf]
        while stack:
            row = stack.pop()
            if row is not leaf:
                if row.uuid in seen:
                    continue
                seen.add(row.uuid)
                chain.append(row)
            below = sorted(children.get(row.uuid, []), key=self._stamp)
            for child in reversed(below):
                if child.uuid not in seen:
                    stack.append(child)

    def _stamp(self, row: Row) -> str:
        return row.timestamp

    def _recover_parallel(self, messages: Mapping[str, Row], chain: list[Row], seen: set[str]) -> list[Row]:
        """mAr: строки одного message.id и их tool_result, не лежащие на цепочке листа."""
        on_chain: list[Row] = []
        for row in chain:
            if row.kind == RecordType.ASSISTANT:
                on_chain.append(row)

        if not on_chain:
            return chain

        by_response: dict[str, list[Row]] = {}
        results: list[Row] = []
        for row in messages.values():
            if row.kind == RecordType.ASSISTANT and row.message_id is not None:
                by_response.setdefault(row.message_id, []).append(row)
                continue
            if row.kind == RecordType.USER and self._kit.has_block(row, BlockType.TOOL_RESULT):
                results.append(row)

        attached = self._attach_results(messages, results, by_response)
        owners = self._call_owners(by_response)
        order: dict[str, int] = {}
        for position, uuid in enumerate(messages):
            order[uuid] = position

        answered: set[str] = set()
        for row in chain:
            answered.update(self._kit.tool_result_ids(row))

        index: dict[str, int] = {}
        for position, row in enumerate(chain):
            index[row.uuid] = position

        children: dict[str, list[Row]] = {}
        for row in messages.values():
            if row.parent is not None:
                children.setdefault(row.parent, []).append(row)

        rank: dict[str, float] = {}
        done: set[str] = set()
        found: list[_Recovery] = []

        for first in on_chain:
            response = first.message_id
            if response is None or response in done:
                continue
            done.add(response)
            recovery = self._recover_response(
                first, by_response.get(response, [first]), chain, seen, attached, owners, order, answered, index, children, rank
            )
            if recovery is not None:
                found.append(recovery)

        if not found:
            return chain

        self._notes.append(f"parallel rows recovered for {len(found)} response(s) off the leaf chain")
        return self._weave(chain, self._merge(found, order, rank), order, rank)

    def _attach_results(
        self, messages: Mapping[str, Row], results: Sequence[Row], by_response: Mapping[str, Sequence[Row]]
    ) -> dict[str, list[Row]]:
        attached: dict[str, list[Row]] = {}
        loose: list[Row] = []

        for row in results:
            if row.parent is not None:
                self._attach(attached, row.parent, row)
            parent_calls: set[str] = set()
            if row.parent is not None and row.parent in messages:
                parent_calls = set(self._kit.tool_use_ids(messages[row.parent]))
            ids = self._kit.tool_result_ids(row)
            if self._kit.all_in(ids, parent_calls):
                continue
            loose.append(row)

        if not loose:
            return attached

        owners = self._call_owners(by_response)
        for row in loose:
            source = row.text(Key.SOURCE_ASSISTANT)
            if source is not None and source != row.parent and source in messages and self._same_lane(row, messages[source]):
                self._attach(attached, source, row)
            for name in self._kit.tool_result_ids(row):
                owner = owners.get(name)
                if owner is not None and owner.uuid != row.parent and self._same_lane(row, owner):
                    self._attach(attached, owner.uuid, row)

        return attached

    def _attach(self, attached: dict[str, list[Row]], key: str, row: Row) -> None:
        rows = attached.setdefault(key, [])
        for known in rows:
            if known.uuid == row.uuid:
                return

        rows.append(row)

    def _call_owners(self, by_response: Mapping[str, Sequence[Row]]) -> dict[str, Row | None]:
        owners: dict[str, Row | None] = {}

        for rows in by_response.values():
            for row in rows:
                for name in self._kit.tool_use_ids(row):
                    if name not in owners:
                        owners[name] = row
                        continue
                    known = owners[name]
                    if known is not None and known.message_id == row.message_id:
                        owners[name] = row
                        continue
                    owners[name] = None

        return owners

    def _same_lane(self, first: Row, second: Row) -> bool:
        return first.sidechain == second.sidechain and first.raw.get(Key.AGENT) == second.raw.get(Key.AGENT)

    def _recover_response(
        self,
        first: Row,
        rows: Sequence[Row],
        chain: Sequence[Row],
        seen: set[str],
        attached: Mapping[str, Sequence[Row]],
        owners: Mapping[str, Row | None],
        order: Mapping[str, int],
        answered: set[str],
        index: Mapping[str, int],
        children: Mapping[str, Sequence[Row]],
        rank: dict[str, float],
    ) -> _Recovery | None:
        response = first.message_id
        own = self._kit.uuids(rows)
        missing: list[Row] = []
        for row in rows:
            if row.uuid not in seen:
                missing.append(row)

        direct: list[Row] = []
        distant: list[Row] = []
        taken: set[str] = set()

        for row in rows:
            for result in attached.get(row.uuid, []):
                if result.uuid in seen or result.uuid in taken:
                    continue
                taken.add(result.uuid)
                if result.parent is not None and result.parent in own:
                    direct.append(result)
                    continue
                distant.append(result)

        if distant:
            covered = set(answered)
            for result in direct:
                covered.update(self._kit.tool_result_ids(result))
            distant = self._ranked(distant, order, rank)
            ceiling = 0
            for row in rows:
                ceiling = max(ceiling, order.get(row.uuid, LoadLimit.LAST))
            for result in distant:
                names = self._kit.tool_result_ids(result)
                if not self._answers_open_call(names, covered, owners, own):
                    continue
                covered.update(names)
                rank[result.uuid] = max(order.get(result.uuid, LoadLimit.LAST), ceiling + 0.5)
                direct.append(result)

        recovered = [*missing, *direct]
        for row in recovered:
            seen.add(row.uuid)

        start = index[first.uuid]
        end = start + 1
        for row in rows:
            position = index.get(row.uuid)
            if position is not None and position >= end:
                end = position + 1
        while end < len(chain) and self._kit.continues(chain[end], response):
            end += 1

        anchored: dict[str, list[Row]] = {}
        if self._calls_tools(rows):
            sources = list(rows)
            known = set(own)
            for row in rows:
                for result in attached.get(row.uuid, []):
                    if result.uuid in seen and result.uuid not in known:
                        known.add(result.uuid)
                        sources.append(result)
            for source in sources:
                self._pull_tails(source, recovered, children, seen, order, rank)
            for row in chain[start + 1 : end]:
                if not self._kit.delivery(row):
                    continue
                tails: list[Row] = []
                self._pull_tails(row, tails, children, seen, order, rank)
                if tails:
                    anchored[row.uuid] = tails

        if not recovered and not anchored:
            return None

        recovered = self._ranked(recovered, order, rank)
        return _Recovery(start=start, end=end, recovered=recovered, anchored=anchored)

    def _calls_tools(self, rows: Sequence[Row]) -> bool:
        for row in rows:
            if self._kit.has_block(row, BlockType.TOOL_USE):
                return True

        return False

    def _ranked(self, rows: Sequence[Row], order: Mapping[str, int], rank: Mapping[str, float]) -> list[Row]:
        """Сортировка Ye: по назначенному месту, затем по порядку файла."""
        keyed: list[tuple[tuple[float, int], int, Row]] = []
        for position, row in enumerate(rows):
            keyed.append((self._rank(row, order, rank), position, row))

        keyed.sort()
        result: list[Row] = []
        for _, _, row in keyed:
            result.append(row)

        return result

    def _answers_open_call(
        self, names: Sequence[str], covered: set[str], owners: Mapping[str, Row | None], own: set[str]
    ) -> bool:
        for name in names:
            owner = owners.get(name)
            if name not in covered and owner is not None and owner.uuid in own:
                return True

        return False

    def _pull_tails(
        self,
        source: Row,
        into: list[Row],
        children: Mapping[str, Sequence[Row]],
        seen: set[str],
        order: Mapping[str, int],
        rank: dict[str, float],
    ) -> None:
        inherited = rank.get(source.uuid)

        for child in children.get(source.uuid, []):
            for row in self._linear_tail(child, source, children, seen):
                seen.add(row.uuid)
                into.append(row)
                if inherited is not None:
                    rank[row.uuid] = max(order.get(row.uuid, LoadLimit.LAST), inherited)

    def _linear_tail(self, start: Row, source: Row, children: Mapping[str, Sequence[Row]], seen: set[str]) -> list[Row]:
        """qfe @210466315: неразветвлённый хвост из вложений/system/meta-user до конца ветки."""
        tail: list[Row] = []
        cursor: Row | None = start

        while cursor is not None:
            if cursor.uuid in seen:
                return []
            if cursor.raw.get(Key.SIDECHAIN) != source.raw.get(Key.SIDECHAIN) or not self._kit.delivery(cursor):
                return []
            tail.append(cursor)
            below: list[Row] = []
            for row in children.get(cursor.uuid, []):
                if row.uuid not in seen:
                    below.append(row)
            if len(below) > 1:
                return []
            cursor = None
            if below:
                cursor = below[0]

        return tail

    def _rank(self, row: Row, order: Mapping[str, int], rank: Mapping[str, float]) -> tuple[float, int]:
        place = order.get(row.uuid, LoadLimit.LAST)
        return rank.get(row.uuid, place), place

    def _merge(self, found: list[_Recovery], order: Mapping[str, int], rank: Mapping[str, float]) -> list[_Recovery]:
        merged: list[_Recovery] = []

        starts: list[tuple[int, int]] = []
        for position, item in enumerate(found):
            starts.append((item.start, position))

        for _, position in sorted(starts):
            item = found[position]
            if not merged or item.start >= merged[-1].end:
                merged.append(item)
                continue
            last = merged[-1]
            recovered = self._ranked([*last.recovered, *item.recovered], order, rank)
            anchored: dict[str, Sequence[Row]] = dict(last.anchored)
            for uuid, tails in item.anchored.items():
                anchored[uuid] = [*anchored.get(uuid, []), *tails]
            merged[-1] = _Recovery(start=last.start, end=max(last.end, item.end), recovered=recovered, anchored=anchored)

        return merged

    def _weave(
        self, chain: Sequence[Row], found: Sequence[_Recovery], order: Mapping[str, int], rank: Mapping[str, float]
    ) -> list[Row]:
        result: list[Row] = []
        copied = 0

        for item in found:
            result.extend(chain[copied : item.start + 1])
            placed = 0
            for position in range(item.start + 1, item.end):
                row = chain[position]
                place = order.get(row.uuid, LoadLimit.LAST)
                while placed < len(item.recovered) and self._rank(item.recovered[placed], order, rank)[0] < place:
                    result.append(item.recovered[placed])
                    placed += 1
                result.append(row)
                result.extend(item.anchored.get(row.uuid, []))
            result.extend(item.recovered[placed:])
            copied = item.end

        result.extend(chain[copied:])
        return result


class SplitRegroup:
    """Перегруппировка расщеплённого ответа (opt @211643992, YZe @207682803).

    Собирает строки одного message.id, разорванные вставками, в непрерывный
    блок и переносит tool_result, оказавшийся раньше своего tool_use.
    """

    def __init__(self, kit: RowKit) -> None:
        self._kit = kit

    def run(self, rows: Sequence[Row]) -> list[Row]:
        first: dict[str, int] = {}
        last: dict[str, int] = {}
        count: dict[str, int] = {}
        owner: dict[str, str] = {}

        for position, row in enumerate(rows):
            response = self._response(row)
            if response is None:
                continue
            first.setdefault(response, position)
            last[response] = position
            count[response] = count.get(response, 0) + 1
            for name in self._kit.tool_use_ids(row):
                known = owner.get(name)
                if known is None or known == response:
                    owner[name] = response
                    continue
                owner[name] = ""

        if not first:
            return list(rows)

        mode: dict[str, str] = {}
        for response, start in first.items():
            mode[response] = self._mode(rows, response, start, last[response], count[response])

        early: dict[int, str] = {}
        moved: dict[str, list[int]] = {}
        for position, row in enumerate(rows):
            self._mark_early(rows, position, row, owner, mode, first, early, moved)

        if "split" not in mode.values() and not moved:
            return list(rows)

        result: list[Row] = []
        used: set[int] = set()
        for position in range(len(rows)):
            if position not in early:
                self._emit(rows, position, first, last, mode, early, moved, result, used)

        if len(result) != len(rows) or len(used) != len(rows):
            return list(rows)

        return result

    def _response(self, row: Row) -> str | None:
        """RX."""
        if row.kind != RecordType.ASSISTANT or self._kit.virtual(row):
            return None

        return row.message_id

    def _role(self, row: Row, response: str) -> str:
        if row.kind == RecordType.ASSISTANT:
            if self._response(row) == response:
                return "block"
            return "boundary"

        if row.kind == RecordType.USER:
            if self._kit.tool_result_ids(row):
                return "result"
            if self._kit.companion(row):
                return "delivery"
            return "boundary"

        if row.kind == RecordType.SYSTEM and (self._kit.boundary(row) or self._kit.local_command(row)):
            return "boundary"

        return "delivery"

    def _mode(self, rows: Sequence[Row], response: str, start: int, end: int, size: int) -> str:
        mode = "whole"
        if end - start + 1 == size:
            return mode

        for position in range(start + 1, end):
            role = self._role(rows[position], response)
            if role == "boundary":
                return "fenced"
            if role == "delivery":
                mode = "split"

        return mode

    def _mark_early(
        self,
        rows: Sequence[Row],
        position: int,
        row: Row,
        owner: Mapping[str, str],
        mode: Mapping[str, str],
        first: Mapping[str, int],
        early: dict[int, str],
        moved: dict[str, list[int]],
    ) -> None:
        names = self._kit.tool_result_ids(row)
        if not names:
            return

        owners: set[str | None] = set()
        for name in names:
            owners.add(owner.get(name))

        if len(owners) != 1:
            return

        response = owners.pop()
        if not response or mode.get(response) == "fenced":
            return

        start = first[response]
        if position >= start:
            return

        for between in range(position + 1, start):
            other = rows[between]
            if other.kind != RecordType.ASSISTANT and self._role(other, response) == "boundary":
                return

        places = moved.setdefault(response, [])
        early[position] = response
        places.append(position)
        follower = position + 1
        while follower < start and self._role(rows[follower], response) == "delivery":
            early[follower] = response
            places.append(follower)
            follower += 1

    def _emit(
        self,
        rows: Sequence[Row],
        position: int,
        first: Mapping[str, int],
        last: Mapping[str, int],
        mode: Mapping[str, str],
        early: Mapping[int, str],
        moved: Mapping[str, Sequence[int]],
        result: list[Row],
        used: set[int],
    ) -> None:
        if position in used:
            return

        row = rows[position]
        response = self._response(row)
        split = response is not None and mode.get(response) == "split"
        pulled: Sequence[int] = ()
        if response is not None:
            pulled = moved.get(response, ())

        if response is None or first[response] != position or (not split and not pulled):
            result.append(row)
            used.add(position)
            return

        deferred: list[int] = []
        for inner in range(position, last[response] + 1):
            if inner in used or inner in early:
                continue
            if not split or self._response(rows[inner]) == response:
                result.append(rows[inner])
                used.add(inner)
                continue
            deferred.append(inner)

        for inner in pulled:
            result.append(rows[inner])
            used.add(inner)

        for inner in deferred:
            self._emit(rows, inner, first, last, mode, early, moved, result, used)


class ToolUseFilter:
    """Незакрытые tool_use (BFe @211676646) в режиме обычного возобновления.

    Убирает строки assistant, все вызовы которых остались без результата, и
    собирает id незакрытых вызовов хвоста — от конца списка до последнего
    настоящего промпта пользователя. Режимы dropSiblingBlocks и
    shutdownUnwind включаются только автопродолжением и не моделируются.
    """

    def __init__(self, kit: RowKit) -> None:
        self._kit = kit

    def unresolved(self, rows: Sequence[Row], resolved: set[str]) -> set[str]:
        used: set[str] = set()
        answered: set[str] = set()

        for row in rows:
            used.update(self._kit.tool_use_ids(row))
            answered.update(self._kit.tool_result_ids(row))

        return used - answered - resolved

    def trailing(self, rows: Sequence[Row], unresolved: set[str]) -> list[str]:
        found: list[str] = []
        past_assistant = False

        for row in reversed(rows):
            if row.kind in (RecordType.SYSTEM, RecordType.PROGRESS, RecordType.ATTACHMENT):
                continue
            if row.kind == RecordType.USER:
                if self._kit.has_block(row, BlockType.TOOL_RESULT):
                    continue
                if row.flag(Key.SHUTDOWN):
                    continue
                if not past_assistant and self._kit.companion(row):
                    continue
                break
            if row.kind != RecordType.ASSISTANT:
                continue
            past_assistant = True
            for name in self._kit.tool_use_ids(row):
                if name in unresolved and name not in found:
                    found.append(name)

        return found

    def run(self, rows: Sequence[Row], resolved: set[str]) -> list[Row]:
        unresolved = self.unresolved(rows, resolved)
        if not unresolved:
            return list(rows)

        kept: list[Row] = []
        for row in rows:
            names = self._kit.tool_use_ids(row)
            if self._kit.all_in(names, unresolved):
                continue
            kept.append(row)

        return kept


class ThinkingFilter:
    """Фильтры ответа без содержимого (pre @211751186: A_n @211754219 и x_n @211751730)."""

    def __init__(self, kit: RowKit) -> None:
        self._kit = kit

    def run(self, rows: Sequence[Row]) -> list[Row]:
        current = list(rows)

        while True:
            filtered = self._drop_blank(self._drop_orphan_thinking(current))
            if len(filtered) == len(current):
                return current
            current = filtered

    def _drop_orphan_thinking(self, rows: Sequence[Row]) -> list[Row]:
        solid: set[str] = set()
        for row in rows:
            if row.kind != RecordType.ASSISTANT or row.message_id is None:
                continue
            if self._kit.has_plain_block(row.blocks):
                solid.add(row.message_id)

        substantive = self._substantive_ids(rows)
        keep: dict[int, bool] = {}

        for position in range(len(rows) - 1, -1, -1):
            row = rows[position]
            blocks = row.blocks
            if row.kind != RecordType.ASSISTANT or not blocks or self._kit.has_plain_block(blocks):
                keep[position] = True
                continue
            if row.message_id is not None and row.message_id in solid:
                keep[position] = True
                continue
            keep[position] = self._bridged(rows, position, keep, substantive)

        kept: list[Row] = []
        for position, row in enumerate(rows):
            if keep[position]:
                kept.append(row)

        return kept

    def _bridged(self, rows: Sequence[Row], position: int, keep: Mapping[int, bool], substantive: set[str]) -> bool:
        """Ответ только из thinking остаётся, если следующий ответ продолжает оборванное размышление."""
        follower = position + 1
        while follower < len(rows) and (self._kit.invisible(rows[follower]) or self._unsigned(rows[follower])):
            follower += 1

        if follower >= len(rows):
            return False

        after = rows[follower]
        if after.kind != RecordType.ASSISTANT or not after.flag("resumedFromIncompleteThinking") or not keep[follower]:
            return False

        if self._kit.blank(after.blocks) and not (after.message_id is not None and after.message_id in substantive):
            return False

        return not self._unsigned(rows[position])

    def _unsigned(self, row: Row) -> bool:
        """N_e: ответ из одних thinking, среди которых есть блок без подписи."""
        blocks = row.blocks
        if row.kind != RecordType.ASSISTANT or not blocks:
            return False

        if self._kit.has_plain_block(blocks):
            return False

        for block in blocks:
            if block.get(Key.TYPE) == BlockType.THINKING and not block.get("signature"):
                return True

        return False

    def _substantive_ids(self, rows: Sequence[Row]) -> set[str]:
        """C_n."""
        found: set[str] = set()
        for row in rows:
            if row.kind != RecordType.ASSISTANT or row.message_id is None:
                continue
            if self._kit.has_substantive(row.blocks):
                found.add(row.message_id)

        return found

    def _drop_blank(self, rows: Sequence[Row]) -> list[Row]:
        substantive = self._substantive_ids(rows)
        kept: list[Row] = []

        for row in rows:
            blank = row.kind == RecordType.ASSISTANT and bool(row.blocks) and self._kit.blank(row.blocks)
            if blank and not (row.message_id is not None and row.message_id in substantive):
                continue
            kept.append(row)

        return kept


class TurnClassifier:
    """Классификация конца хода (d1e, Enr @210496260) без веток под переменными окружения."""

    def __init__(self, kit: RowKit) -> None:
        self._kit = kit
        self._terminal_tools = frozenset(("SendUserMessage", "Brief", "SendUserFile"))

    def classify(self, rows: Sequence[Row]) -> TurnState:
        skipped: list[Row] = []
        kind = self._kind(rows, skipped)
        first: Row | None = None
        if skipped:
            first = skipped[0]

        return TurnState(kind=kind, skipped_api_error=first)

    def command_tail(self, rows: Sequence[Row], position: int) -> bool:
        """x6: строка завершает запись локальной команды."""
        if self._command_role(rows[position]) is None:
            return False

        after_record = False
        all_meta = True

        for cursor in range(position, -1, -1):
            row = rows[cursor]
            if row.kind in (RecordType.SYSTEM, RecordType.PROGRESS, RecordType.ATTACHMENT):
                continue
            role = self._command_role(row)
            if role == "caveat":
                return True
            if role is None or after_record:
                break
            after_record = role == "record"
            all_meta = all_meta and row.kind == RecordType.USER and row.flag(Key.META)

        return all_meta

    def _kind(self, rows: Sequence[Row], skipped: list[Row]) -> TurnKind:
        last = -1
        queued = False

        for position in range(len(rows) - 1, -1, -1):
            row = rows[position]
            if row.kind in (RecordType.SYSTEM, RecordType.PROGRESS):
                continue
            if self._kit.queued_notification(row):
                queued = True
                continue
            if self._transient_error(row):
                skipped.append(row)
                continue
            last = position
            break

        if last == -1:
            return TurnKind.NONE

        row = rows[last]
        if row.kind == RecordType.ASSISTANT:
            return TurnKind.NONE

        if row.kind == RecordType.ATTACHMENT:
            return self._behind_attachments(rows, last, skipped)

        if row.kind != RecordType.USER:
            return TurnKind.NONE

        if self.command_tail(rows, last):
            return TurnKind.NONE

        if row.flag(Key.META) or row.flag("isCompactSummary"):
            return TurnKind.NONE

        if self._interrupt_text(row):
            return self._by_shutdown(row)

        if self._tool_result_row(row):
            if self._ends_turn(rows, last, row):
                return TurnKind.NONE
            return TurnKind.INTERRUPTED_TURN

        if self._kit.task_notification(row) or queued:
            return TurnKind.INTERRUPTED_TURN

        return TurnKind.INTERRUPTED_PROMPT

    def _behind_attachments(self, rows: Sequence[Row], last: int, skipped: list[Row]) -> TurnKind:
        uncommitted = False

        for position in range(last, -1, -1):
            row = rows[position]
            if row.kind in (RecordType.SYSTEM, RecordType.PROGRESS, RecordType.ATTACHMENT):
                if row.attachment_kind == Subtype.MAX_TURNS and row.attachment.get("exitCommitted") is False:
                    uncommitted = True
                continue
            if self._kit.queued_notification(row):
                continue
            if row.kind == RecordType.ASSISTANT:
                if self._transient_error(row):
                    skipped.append(row)
                    continue
                return TurnKind.NONE
            if row.kind == RecordType.USER:
                verdict = self._user_behind(rows, position, row)
                if verdict is not None:
                    return verdict
            if uncommitted:
                return TurnKind.ENDED_AT_MAX_TURNS
            return TurnKind.INTERRUPTED_TURN

        return TurnKind.INTERRUPTED_TURN

    def _user_behind(self, rows: Sequence[Row], position: int, row: Row) -> TurnKind | None:
        if row.flag("isCompactSummary"):
            return TurnKind.NONE

        if self._interrupt_text(row):
            return self._by_shutdown(row)

        if self.command_tail(rows, position):
            return TurnKind.NONE

        if self._tool_result_row(row) and self._ends_turn(rows, position, row):
            return TurnKind.NONE

        return None

    def _all_turn_ended(self, blocks: Sequence[Raw]) -> bool:
        """Каждый блок — tool_result с текстом «ход завершён, чтобы доставить сообщение»."""
        for block in blocks:
            if block.get(Key.TYPE) != BlockType.TOOL_RESULT:
                return False
            if block.get(Key.CONTENT) != Phrase.TURN_ENDED:
                return False

        return True

    def _ends_turn(self, rows: Sequence[Row], position: int, row: Row) -> bool:
        """BXt, NXt, FXt, $Xt: результат, после которого ход считается завершённым."""
        blocks = row.blocks
        if blocks and blocks[0].get(Key.TYPE) == BlockType.TOOL_RESULT and self._terminal_call(rows, position, blocks[0]):
            return True

        if blocks and self._all_turn_ended(blocks):
            return True

        for cursor in range(position, -1, -1):
            other = rows[cursor]
            if other.kind == RecordType.ASSISTANT:
                break
            if other.kind == RecordType.USER and self._tool_result_row(other) and other.flag("toolDenialEndsTurn"):
                return True

        result = row.raw.get("toolUseResult")
        return isinstance(result, dict) and result.get("backgroundedByTurnAbort") is True

    def _terminal_call(self, rows: Sequence[Row], position: int, result: Raw) -> bool:
        call = result.get(Key.TOOL_USE_ID)

        for cursor in range(position - 1, -1, -1):
            row = rows[cursor]
            if row.kind != RecordType.ASSISTANT:
                continue
            for block in row.blocks:
                if block.get(Key.TYPE) == BlockType.TOOL_USE and block.get(Key.ID) == call:
                    return block.get(Key.NAME) in self._terminal_tools

        return False

    def _tool_result_row(self, row: Row) -> bool:
        """JK."""
        blocks = row.blocks
        if blocks and blocks[0].get(Key.TYPE) == BlockType.TOOL_RESULT:
            return True

        return bool(row.raw.get("toolUseResult"))

    def _interrupt_text(self, row: Row) -> bool:
        """LXt."""
        return self._kit.single_text(row.content) in (Phrase.INTERRUPTED, Phrase.INTERRUPTED_TOOL)

    def _by_shutdown(self, row: Row) -> TurnKind:
        if row.flag(Key.SHUTDOWN):
            return TurnKind.INTERRUPTED_TURN

        return TurnKind.NONE

    def _transient_error(self, row: Row) -> bool:
        """HXt: строка ошибки API, не являющаяся отказом модели."""
        if row.kind != RecordType.ASSISTANT or not row.flag(Key.API_ERROR):
            return False

        if row.message.get("stop_reason") == "refusal":
            return False

        return row.raw.get("apiError") not in ("dlp_request_denied", "safety_monitor_blocked")

    def _command_role(self, row: Row) -> str | None:
        """f1e, v9r."""
        if row.kind != RecordType.USER or "promptSource" in row.raw:
            return None

        text: str | None = None
        content = row.content
        if isinstance(content, str):
            text = content
        for block in row.blocks:
            candidate = block.get(Key.TEXT)
            if block.get(Key.TYPE) == BlockType.TEXT and isinstance(candidate, str):
                text = candidate

        if text is None:
            return None

        for tag in CommandTag:
            if not text.startswith(tag):
                continue
            if tag is CommandTag.CAVEAT and not row.flag(Key.META):
                return None
            return tag.role()

        return None


class SyntheticRows:
    """Записи, которые возобновление создаёт само (IXt @210495662, bc/Jyn @211625810).

    uuid у них в бинарнике случайные; здесь — порядковые, чтобы результат был
    воспроизводим.
    """

    def __init__(self, session: str) -> None:
        self._session = session
        self._number = 0

    def tool_result(self, call: str, source: Row) -> Row:
        raw: dict[str, Json] = {
            Key.PARENT: None,
            Key.SIDECHAIN: False,
            Key.TYPE: RecordType.USER.value,
            Key.MESSAGE: {
                "role": "user",
                Key.CONTENT: [
                    {
                        Key.TYPE: BlockType.TOOL_RESULT.value,
                        Key.CONTENT: Phrase.SESSION_ENDED.value,
                        "is_error": True,
                        Key.TOOL_USE_ID: call,
                    }
                ],
            },
            Key.UUID: self._uuid(),
            Key.TIMESTAMP: "",
            "toolUseResult": Phrase.SESSION_ENDED.value,
            "toolDenialKind": Phrase.INTERRUPTED_KIND.value,
            Key.SOURCE_ASSISTANT: source.uuid,
            Key.SESSION: self._session,
        }
        return Row(line=0, raw=raw)

    def no_response(self) -> Row:
        raw: dict[str, Json] = {
            Key.PARENT: None,
            Key.SIDECHAIN: False,
            Key.TYPE: RecordType.ASSISTANT.value,
            Key.UUID: self._uuid(),
            Key.TIMESTAMP: "",
            Key.MESSAGE: {
                Key.ID: self._uuid(),
                Key.MODEL: Synthetic.MODEL.value,
                "role": "assistant",
                "stop_reason": "stop_sequence",
                "stop_sequence": "",
                Key.TYPE: "message",
                Key.CONTENT: [{Key.TYPE: BlockType.TEXT.value, Key.TEXT: Phrase.NO_RESPONSE.value}],
            },
            Key.API_ERROR: False,
            Key.SESSION: self._session,
        }
        return Row(line=0, raw=raw)

    def stamped_id(self) -> str:
        return self._uuid()

    def _uuid(self) -> str:
        self._number += 1
        return Phrase.SYNTHETIC_UUID.format(number=self._number)


class Deserializer:
    """Починка цепочки для возобновления (zXt @210490459) в режиме `-p --resume`.

    Вход — цепочка ChainBuilder'а и точка отката; выход — список, с которым
    процесс продолжает работу. Режим: автопродолжение выключено (нет
    CLAUDE_CODE_RESUME_INTERRUPTED_TURN), отложенного tool_use нет, флаг
    tengu_foamy_spring включён по умолчанию.
    """

    def __init__(self, kit: RowKit, notes: list[str], now_ms: float) -> None:
        self._kit = kit
        self._notes = notes
        self._now_ms = now_ms
        self._text = JsText()
        self._repair = RowRepair()
        self._regroup = SplitRegroup(kit)
        self._tools = ToolUseFilter(kit)
        self._thinking = ThinkingFilter(kit)
        self._turns = TurnClassifier(kit)
        self._ephemeral = frozenset(
            (
                "compaction_reminder",
                "companion_intro",
                "echo_activities",
                "pen_mode_enter",
                "pen_mode_exit",
                "verify_plan_reminder",
                "fold_nudge",
                "context_tip",
                "audio_transcript",
                "new_file",
                "new_directory",
            )
        )

    def run(self, chain: Sequence[Row], rewind_anchor: str, synthetic: SyntheticRows) -> tuple[list[Row], TurnKind]:
        rows = list(self._prepared(chain, synthetic))
        regrouped = self._regroup.run(rows)
        anchored = self._ends_at(rows, rewind_anchor)

        unresolved = self._tools.unresolved(regrouped, set())
        trailing = self._tools.trailing(regrouped, unresolved)
        current = self._thinking.run(self._tools.run(regrouped, set()))

        state = self._turns.classify(current)
        kind = state.kind
        if kind is TurnKind.ENDED_AT_MAX_TURNS:
            kind = TurnKind.NONE

        suppressed = kind is not TurnKind.NONE and (anchored or self._ends_at(current, rewind_anchor))
        if not suppressed and kind is not TurnKind.NONE and self._stale_error(state, regrouped):
            suppressed = True

        outcome = kind
        answered = False
        awaiting_reply = False

        if suppressed:
            outcome = TurnKind.NONE
            self._notes.append(f"turn state {kind.value} suppressed: the tail is the rewind anchor or a stale API error")

        if not suppressed and kind is TurnKind.INTERRUPTED_TURN:
            if trailing:
                calls = self._last_response_calls(regrouped, set(trailing), reset_on_error=False)
                current = self._with_results(regrouped, set(trailing), calls, synthetic)
                answered = True
            awaiting_reply = True
            outcome = TurnKind.INTERRUPTED_PROMPT

        if not answered and trailing and not anchored and not self._ends_at(current, rewind_anchor):
            calls = self._last_response_calls(regrouped, set(trailing), reset_on_error=True)
            if calls:
                current = self._with_results(regrouped, calls, calls, synthetic)

        self._close_turn(current, outcome, kind, awaiting_reply, synthetic)
        self._note_dropped(regrouped, current)
        return current, outcome

    def _prepared(self, chain: Sequence[Row], synthetic: SyntheticRows) -> Iterator[Row]:
        """Шаги zXt до перегруппировки: RXt, dun, mY, штамп id, onr, snr."""
        retracted = self._retracted(chain)

        for row in chain:
            if row.kind in (RecordType.USER, RecordType.ASSISTANT):
                repaired = self._repair.message(row)
                if repaired is None:
                    self._notes.append(f"line {row.line}: unreadable {row.kind} row dropped at resume")
                    continue
                row = repaired
            if row.kind != RecordType.SYSTEM and row.uuid[: LoadLimit.RETRACT_PREFIX] in retracted:
                self._notes.append(f"line {row.line}: row retracted by model_refusal_fallback")
                continue
            if row.kind == RecordType.ATTACHMENT and not self._valid_attachment(row):
                self._notes.append(f"line {row.line}: attachment with a malformed payload dropped")
                continue
            if row.kind == RecordType.ATTACHMENT and row.attachment_kind in self._ephemeral:
                continue
            if row.kind == RecordType.ASSISTANT and row.message_id is None and Key.REQUEST_ID not in row.raw:
                row = row.with_message_id(synthetic.stamped_id())
            cleaned = self._without_broken_text(row)
            if cleaned is not None:
                yield cleaned

    def _retracted(self, chain: Sequence[Row]) -> set[str]:
        found: set[str] = set()

        for row in chain:
            if row.kind != RecordType.SYSTEM or row.subtype != Subtype.REFUSAL_FALLBACK:
                continue
            names = row.raw.get("retractedMessageUuids")
            if not isinstance(names, list):
                continue
            for name in names:
                if isinstance(name, str):
                    found.add(name[: LoadLimit.RETRACT_PREFIX])

        return found

    def _valid_attachment(self, row: Row) -> bool:
        """str @210464923."""
        payload = row.raw.get(Key.ATTACHMENT)
        if not isinstance(payload, dict) or not isinstance(payload.get(Key.TYPE), str):
            return False

        kind = payload.get(Key.TYPE)
        content = payload.get(Key.CONTENT)

        if kind == "invoked_skills":
            skills = payload.get("skills")
            return self._every(skills, dict, LoadLimit.LAST)

        if kind == "hook_success":
            return isinstance(content, str)

        if kind == "skill_listing":
            names = payload.get("names")
            if names is None:
                return True
            if not isinstance(names, list) or len(names) > LoadLimit.SKILL_NAMES:
                return False
            return self._every(names, str, LoadLimit.SKILL_NAME_CHARS)

        if kind == "hook_additional_context":
            return self._every(content, str, LoadLimit.LAST)

        if kind in ("task_reminder", "todo_reminder"):
            return self._every(content, dict, LoadLimit.LAST)

        if kind in ("file", "already_read_file"):
            return isinstance(content, dict)

        return True

    def _every(self, items: Json, kind: type, longest: int) -> bool:
        """Значение — список, все элементы которого данного типа и не длиннее longest."""
        if not isinstance(items, list):
            return False

        for item in items:
            if not isinstance(item, kind):
                return False
            if isinstance(item, str) and len(item) > longest:
                return False

        return True

    def _without_broken_text(self, row: Row) -> Row | None:
        """snr: text-блоки с нестроковым text убираются; опустевшая строка исчезает."""
        if row.kind not in (RecordType.USER, RecordType.ASSISTANT):
            return row

        content = row.content
        if not isinstance(content, list):
            return row

        kept: list[Json] = []
        for block in content:
            if isinstance(block, dict) and block.get(Key.TYPE) == BlockType.TEXT and not isinstance(block.get(Key.TEXT), str):
                continue
            kept.append(block)

        if len(kept) == len(content):
            return row

        if not kept:
            return None

        return row.with_content(kept)

    def _ends_at(self, rows: Sequence[Row], anchor: str) -> bool:
        """i1e: последняя запись user/assistant — точка отката."""
        if not anchor:
            return False

        for row in reversed(rows):
            if row.kind not in (RecordType.USER, RecordType.ASSISTANT):
                continue
            if row.uuid == anchor:
                return True
            if not self._kit.queued_notification(row):
                return False

        return False

    def _stale_error(self, state: TurnState, regrouped: Sequence[Row]) -> bool:
        """Хвост — транзитная ошибка API старше шести часов (AXt, unr)."""
        row = state.skipped_api_error
        other = self._turns.classify(regrouped).skipped_api_error
        if row is None:
            row = other
        if row is not None and other is not None:
            mine = self._text.epoch_ms(row.timestamp)
            theirs = self._text.epoch_ms(other.timestamp)
            if mine is not None and theirs is not None and theirs > mine:
                row = other

        if row is None:
            return False

        stamp = self._text.epoch_ms(row.timestamp)
        if stamp is None:
            return True

        return abs(self._now_ms - stamp) >= LoadLimit.STALE_API_ERROR_MS

    def _last_response_calls(self, rows: Sequence[Row], wanted: set[str], reset_on_error: bool) -> set[str]:
        """MXt: незакрытые вызовы последнего ответа списка."""
        seen: set[str | None] = set()
        current: str | None = None
        ended = True
        calls: set[str] = set()
        turn_open = True
        turn_classified = False
        had_result = False

        for row in self._thinking.run(rows):
            skipped = self._kit.virtual(row) or (not reset_on_error and self._kit.synthetic_error(row))
            if row.kind == RecordType.ASSISTANT and not skipped:
                if reset_on_error and self._kit.synthetic_error(row):
                    ended = True
                    calls = set()
                    continue
                if turn_open and not turn_classified:
                    if not had_result:
                        seen.clear()
                    turn_classified = True
                own = self._wanted_calls(row, wanted)
                if row.message_id in seen:
                    if not ended and row.message_id == current:
                        calls.update(own)
                    continue
                seen.add(row.message_id)
                current = row.message_id
                ended = False
                calls = own
                turn_open = False
                continue
            if (row.kind == RecordType.USER and not self._kit.virtual(row)) or self._kit.local_command(row):
                if not turn_open:
                    turn_open = True
                    turn_classified = False
                    had_result = False
                if self._kit.has_block(row, BlockType.TOOL_RESULT):
                    had_result = True
                continue
            if reset_on_error and row.kind == RecordType.SYSTEM and row.subtype == Subtype.REFUSAL_FALLBACK:
                ended = True
                calls = set()

        return calls

    def _wanted_calls(self, row: Row, wanted: set[str]) -> set[str]:
        """PXt: вызовы строки, входящие в набор незакрытых."""
        own: set[str] = set()
        for name in self._kit.tool_use_ids(row):
            if name in wanted:
                own.add(name)

        return own

    def _with_results(self, rows: Sequence[Row], resolved: set[str], calls: set[str], synthetic: SyntheticRows) -> list[Row]:
        """IXt: строки с незакрытыми вызовами остаются, вызовам из calls дописывается результат."""
        kept = self._thinking.run(self._tools.run(rows, resolved))
        added: list[Row] = []

        for row in kept:
            for name in self._kit.tool_use_ids(row):
                if name in calls:
                    added.append(synthetic.tool_result(name, row))

        if added:
            self._notes.append(f"{len(added)} interrupted tool_result row(s) synthesized for the last response")

        return [*kept, *added]

    def _close_turn(self, rows: list[Row], outcome: TurnKind, kind: TurnKind, awaiting_reply: bool, synthetic: SyntheticRows) -> None:
        """Ответ-заглушка после хвоста, на котором список кончается не ответом ассистента."""
        last = -1
        for position in range(len(rows) - 1, -1, -1):
            if rows[position].kind not in (RecordType.SYSTEM, RecordType.PROGRESS):
                last = position
                break

        if last == -1:
            return

        tail = rows[last]
        if tail.kind != RecordType.USER and not (awaiting_reply and tail.kind == RecordType.ATTACHMENT):
            return

        if self._turns.command_tail(rows, last):
            return

        if kind is TurnKind.NONE and self._kit.queued_notification(tail):
            return

        rows.insert(last + 1, synthetic.no_response())
        self._notes.append(f"synthetic assistant '{Phrase.NO_RESPONSE.value}' placed after line {tail.line} (turn state {outcome.value})")

    def _note_dropped(self, before: Sequence[Row], after: Sequence[Row]) -> None:
        kept = self._kit.uuids(after)
        for row in before:
            if row.uuid not in kept:
                self._notes.append(f"line {row.line}: {row.kind} row dropped (unresolved tool_use, thinking-only or blank)")


class SessionLoader:
    """Загрузка журнала для возобновления (U_e @210502848 → I_e → tRe → Ass → zXt).

    Собирает из компонентов весь путь: Journal отдаёт строки,
    TranscriptIndex раскладывает и выбирает лист, ChainBuilder строит цепочку,
    Deserializer чинит её. Пользуются SessionMemory, сверка и команда history.
    """

    def __init__(self, journal: Journal, now_ms: float | None = None) -> None:
        self._journal = journal
        self._now_ms = now_ms
        self._kit = RowKit()

    def load(self, upto: int | None = None) -> LoadResult:
        """Загрузить состояние файла на момент после строки upto (весь файл, если None)."""
        notes: list[str] = []
        index = TranscriptIndex(self._kit, notes)

        try:
            for line, entry in self._journal.entries(upto):
                index.take(line, entry)
        except JournalError as exc:
            raise LoaderError(f"loading {self._journal.path} as of line {upto} failed: {exc}") from exc

        skipped = self._journal.unparsed(upto)
        if skipped:
            notes.append(f"{skipped} line(s) did not parse as JSON and were skipped")

        leaves, hints = index.finish()
        builder = ChainBuilder(self._kit, notes)
        leaf = builder.pick_leaf(index.messages, leaves, hints.cleared)

        if leaf is None:
            metadata = index.metadata("", "", (), hints)
            found = hints.cleared and bool(index.messages)
            if found:
                notes.append("explicit empty leaf marker: the session resumes with an empty history, the new prompt is a root")
            if not found:
                notes.append("no rows or no leaf: the binary reports 'No conversation found with session ID'")
            return self._result(found, (), (), metadata, TurnKind.NONE, "", "", skipped, notes)

        chain = builder.build(index.messages, leaf)
        session = leaf.text(Key.SESSION)
        if session is None:
            session = ""

        now_ms = self._now_ms
        if now_ms is None:
            now_ms = time.time() * 1000

        synthetic = SyntheticRows(session)
        history, turn = Deserializer(self._kit, notes, now_ms).run(chain, self._anchor(hints), synthetic)
        history, appended, next_parent = self._chained(history)
        metadata = index.metadata(session, leaf.uuid, chain, hints)
        return self._result(True, history, appended, metadata, turn, leaf.uuid, next_parent, skipped, notes)

    def _anchor(self, hints: LeafHints) -> str:
        if hints.rewound:
            return hints.prompt_leaf

        return ""

    def _chained(self, history: Sequence[Row]) -> tuple[list[Row], list[Row], str]:
        """Привязка новых записей писателем (Dv @211843947, bYe @211960938).

        Уже лежащие в файле записи сдвигают точку привязки только до первой
        новой; дальше цепочку ведут сами новые записи. Запись, которую
        писатель отфильтровывает (xb, $Ar), в привязке не участвует.
        """
        result: list[Row] = []
        appended: list[Row] = []
        pointer: str | None = None
        seen_new = False

        for row in history:
            if row.line != 0:
                result.append(row)
                if not seen_new and self._storable(row):
                    pointer = row.uuid
                continue
            parent = pointer
            source = row.text(Key.SOURCE_ASSISTANT)
            if source is not None:
                parent = source
            row = row.with_parent(parent)
            appended.append(row)
            result.append(row)
            pointer = row.uuid
            seen_new = True

        if pointer is None:
            pointer = ""

        return result, appended, pointer

    def _storable(self, row: Row) -> bool:
        """xb и $Ar: запись дошла бы до файла при повторной записи списка."""
        if row.kind == RecordType.PROGRESS:
            return False

        if row.kind == RecordType.SYSTEM and row.raw.get("persist") is False:
            return False

        if row.kind == RecordType.ATTACHMENT and row.attachment_kind == "hook_success":
            payload = row.attachment
            return bool(payload.get(Key.CONTENT) or payload.get("stdout") or payload.get("stderr"))

        if row.kind in (RecordType.USER, RecordType.ASSISTANT) and isinstance(row.content, list):
            return bool(row.content)

        return True

    def _reappended(self, metadata: SessionMetadata) -> list[str]:
        """Метаданные, которые процесс восстановил (PU @211900093) и повторит в хвосте файла."""
        kinds: list[str] = []
        pairs = (
            (MetaRecord.CUSTOM_TITLE, metadata.custom_title),
            (MetaRecord.AI_TITLE, metadata.ai_title),
            (MetaRecord.TAG, metadata.tag),
            (MetaRecord.AGENT_NAME, metadata.agent_name),
            (MetaRecord.AGENT_COLOR, metadata.agent_color),
            (MetaRecord.AGENT_SETTING, metadata.agent_setting),
            (MetaRecord.MODE, metadata.mode),
            (MetaRecord.PERMISSION_MODE, metadata.permission_mode),
        )

        for record, value in pairs:
            if value:
                kinds.append(record.value)

        if metadata.atis_latch:
            kinds.append(RecordType.ATIS_LATCH.value)

        return kinds

    def _result(
        self,
        found: bool,
        history: Sequence[Row],
        appended: Sequence[Row],
        metadata: SessionMetadata,
        turn: TurnKind,
        leaf: str,
        next_parent: str,
        skipped: int,
        notes: Sequence[str],
    ) -> LoadResult:
        return LoadResult(
            found=found,
            history=tuple(history),
            appended=tuple(appended),
            reappended=tuple(self._reappended(metadata)),
            metadata=metadata,
            turn=turn,
            leaf=leaf,
            next_parent=next_parent,
            skipped_lines=skipped,
            notes=tuple(notes),
        )


class JournalMark(StrEnum):
    """Поля и значения записей журнала, по которым размечаются процессы клиента."""

    FIELD = "operation"
    ENQUEUE = "enqueue"
    DENIAL = "toolDenialKind"
    PRESERVED = "preservedMessages"
    ALL_UUIDS = "allUuids"
    UUIDS = "uuids"
    ANCHOR = "anchorUuid"


@dataclass(frozen=True)
class RequestMoment:
    """Момент обращения к модели: история запроса — записи со строками меньше line.

    answered ложно у запроса, на который в журнале ещё нет ответа (ход не
    завершён); его line — номер строки сразу за концом журнала.
    """

    ordinal: int
    line: int
    answered: bool


class ProcessStarts:
    """Разметка журнала на процессы клиента по записям истории.

    Процесс начинает работу записью queue-operation enqueue. Новым процессом
    она считается в трёх случаях: перед ней стоит cost-state (прежний процесс
    завершился штатно); сразу за ней идут записи, которые возобновление
    дописывает само (синтетический tool_result, ответ «No response
    requested.»); первый промпт за ней подвешен не к последней записанной
    строке цепочки (историю перечитал загрузчик). Убитый процесс, после
    которого загрузчик ничего не изменил, по истории не виден — и список в
    памяти для него совпадает с порядком файла. Создаётся SessionMemory.
    """

    def __init__(self, journal: Journal) -> None:
        self._starts: list[int] = []
        seen: set[str] = set()
        ended = False
        enqueue = 0
        last = ""

        for number, raw in journal.entries():
            row = Row(line=number, raw=raw)

            if row.kind == RecordType.COST_STATE:
                ended = True
                continue

            if row.kind == RecordType.QUEUE and raw.get(JournalMark.FIELD) == JournalMark.ENQUEUE:
                if enqueue == 0:
                    enqueue = number
                continue

            if not self._in_transcript(row) or row.uuid in seen:
                continue

            seen.add(row.uuid)

            if enqueue and self._opens_process(row, ended, last):
                self._starts.append(enqueue)
                ended = False

            enqueue = 0

            if not row.sidechain:
                last = row.uuid

    def cut_for(self, line: int) -> int:
        """Первая строка процесса, записавшего строку line; 0 — журнал начат этим процессом."""
        cut = 0
        for candidate in self._starts:
            if candidate <= line:
                cut = candidate

        return cut

    def cuts(self) -> list[int]:
        return list(self._starts)

    def appended_on_resume(self, row: Row) -> bool:
        """Запись, которую возобновление дописало само: результат прерванного вызова либо ответ-заглушка."""
        if row.raw.get(JournalMark.DENIAL) == Phrase.INTERRUPTED_KIND:
            return True

        if row.kind != RecordType.ASSISTANT:
            return False

        if row.message.get(Key.MODEL) != Synthetic.MODEL:
            return False

        return not row.flag(Key.API_ERROR)

    def _opens_process(self, row: Row, ended: bool, last: str) -> bool:
        """Первая строка истории после enqueue выдаёт начало процесса."""
        if ended:
            return True

        if self.appended_on_resume(row):
            return True

        if row.kind != RecordType.USER or not last:
            return False

        return row.parent != last

    def _in_transcript(self, row: Row) -> bool:
        if row.kind not in tuple(RecordType):
            return False

        return RecordType(row.kind).in_transcript()


class RequestMoments:
    """Моменты обращений к модели, найденные по самой истории, без записей эталона.

    Каждое обращение стоит перед первой записью ассистента с новым message.id
    (синтетические записи и повторно записанные uuid не считаются); если
    журнал кончается промптом или результатом инструмента без ответа, ход не
    завершён и ещё один запрос стоит за последней строкой. Запросы, на
    которые ответ не пришёл (отказ API и повтор), в истории следа не
    оставляют. Создаётся SessionMemory.
    """

    def __init__(self, journal: Journal) -> None:
        self._kit = RowKit()
        self._moments: list[RequestMoment] = []
        seen_uuids: set[str] = set()
        answers: set[str] = set()
        last: Row | None = None

        for number, raw in journal.entries():
            row = Row(line=number, raw=raw)
            if row.kind not in (RecordType.USER, RecordType.ASSISTANT):
                continue
            if row.uuid in seen_uuids:
                continue
            seen_uuids.add(row.uuid)
            last = row
            if not self._answer(row):
                continue
            answer = str(row.message_id)
            if answer in answers:
                continue
            answers.add(answer)
            self._moments.append(RequestMoment(ordinal=len(self._moments) + 1, line=number, answered=True))

        if last is not None and self._awaits_answer(last):
            self._moments.append(
                RequestMoment(ordinal=len(self._moments) + 1, line=journal.line_count + 1, answered=False)
            )

    def moments(self) -> list[RequestMoment]:
        return list(self._moments)

    def following(self, line: int) -> RequestMoment | None:
        """Первый момент на строке line или после неё."""
        for moment in self._moments:
            if moment.line >= line:
                return moment

        return None

    def _answer(self, row: Row) -> bool:
        if row.kind != RecordType.ASSISTANT:
            return False

        if row.message.get(Key.MODEL) == Synthetic.MODEL:
            return False

        return row.message_id is not None

    def _awaits_answer(self, row: Row) -> bool:
        """Последняя запись — промпт, результат инструмента или сводка автосжатия, а не след локальной команды."""
        if row.kind != RecordType.USER:
            return False

        text = self._kit.first_text(row.content)
        if text is None:
            return True

        return not text.startswith(tuple(CommandTag))


@dataclass(frozen=True)
class MemoryWindow:
    """Срез списка сообщений клиента от последней границы сжатия.

    preserved — uuid дословно сохранённого хвоста: записи лежат в журнале до
    границы, а в списке стоят после якоря (сводки).
    """

    rows: Sequence[Row]
    preserved: frozenset[str]


class SessionMemory:
    """Список записей в памяти процесса перед строкой журнала.

    То, что процесс получил от загрузчика при возобновлении (SessionLoader над
    состоянием файла на момент старта процесса), плюс строки, которые он
    записал сам. Повторно записанная строка с уже известным uuid нового
    элемента не создаёт — как Map.set в разборе клиента (rbn @211932344).
    Создаётся над журналом входом командной строки и сверкой; им пользуются
    Assembler, HistoryFacts и модель сжатия.
    """

    def __init__(self, journal: Journal) -> None:
        self._journal = journal
        self._kit = RowKit()
        self._starts = ProcessStarts(journal)
        self._moments = RequestMoments(journal)
        self._loader = SessionLoader(journal)
        self._loads: dict[int, LoadResult] = {}
        self._rows: list[Row] = []
        self._first_line: dict[str, int] = {}

        for number, raw in journal.entries():
            row = Row(line=number, raw=raw)
            if not self._in_transcript(row):
                continue
            self._rows.append(row)
            self._first_line.setdefault(row.uuid, number)

    @property
    def journal(self) -> Journal:
        return self._journal

    @property
    def rows(self) -> Sequence[Row]:
        """Все строки истории журнала в порядке файла, включая повторно записанные."""
        return self._rows

    def moments(self) -> list[RequestMoment]:
        return self._moments.moments()

    def moment_for(self, line: int) -> RequestMoment | None:
        return self._moments.following(line)

    def cuts(self) -> list[int]:
        return self._starts.cuts()

    def cut_for(self, line: int) -> int:
        return self._starts.cut_for(line)

    def loaded(self, cut: int) -> LoadResult:
        """Результат загрузчика для состояния файла перед строкой cut."""
        known = self._loads.get(cut)
        if known is not None:
            return known

        result = self._loader.load(cut - 1)
        self._loads[cut] = result
        return result

    def persisted(self, cut: int) -> list[Row]:
        """Записи, которые клиент дописал при возобновлении: синтетические, идущие первыми после cut."""
        found: list[Row] = []

        for row in self._rows:
            if row.line < cut:
                continue
            if not self._starts.appended_on_resume(row):
                break
            found.append(row)

        return found

    def first_own(self, cut: int) -> Row | None:
        """Первая собственная запись истории процесса (новый промпт)."""
        for row in self._rows:
            if row.line >= cut and not self._starts.appended_on_resume(row):
                return row

        return None

    def rows_before(self, line: int) -> list[Row]:
        """Список в памяти процесса, записавшего строку line, на момент перед ней."""
        cut = self._starts.cut_for(line)
        placed: list[Row] = []
        skipped: set[int] = set()

        if cut:
            placed.extend(self.loaded(cut).history)
            for row in self.persisted(cut):
                skipped.add(row.line)

        own: list[Row] = []
        present: set[str] = set()
        for row in self._rows:
            if row.line < cut or row.line >= line or row.line in skipped:
                continue
            if row.uuid and self._first_line[row.uuid] != row.line:
                continue
            own.append(row)
            present.add(row.uuid)

        known: set[str] = set()
        waiting: dict[str, list[Row]] = {}
        for row in own:
            parent = row.parent
            if parent is not None and parent in present and parent not in known:
                waiting.setdefault(parent, []).append(row)
                continue
            self._place(row, placed, known, waiting)

        placed.extend(self._stranded(own, known))
        return placed

    def window_before(self, line: int) -> MemoryWindow:
        """Срез от последней границы (_i), сохранённый хвост — после якоря (mD)."""
        placed = self.rows_before(line)
        if not placed:
            return MemoryWindow(rows=(), preserved=frozenset())

        start = 0
        for position, row in enumerate(placed):
            if self._kit.boundary(row):
                start = position

        boundary = placed[start]
        uuids: tuple[str, ...] = ()
        anchor = ""
        if self._kit.boundary(boundary):
            uuids, anchor = self._preserved(boundary)

        by_uuid: dict[str, Row] = {}
        for row in placed[:start]:
            by_uuid[row.uuid] = row

        kept: list[Row] = []
        for uuid in uuids:
            if uuid in by_uuid:
                kept.append(by_uuid[uuid])

        rows: list[Row] = []
        for row in placed[start:]:
            rows.append(row)
            if anchor and row.uuid == anchor:
                rows.extend(kept)

        return MemoryWindow(rows=tuple(rows), preserved=frozenset(uuids))

    def _preserved(self, boundary: Row) -> tuple[tuple[str, ...], str]:
        """compactMetadata.preservedMessages: uuid сохранённого хвоста и якорь, за которым он стоит."""
        metadata = boundary.raw.get(Key.COMPACT_METADATA)
        if not isinstance(metadata, dict):
            return (), ""

        messages = metadata.get(JournalMark.PRESERVED)
        if not isinstance(messages, dict):
            return (), ""

        uuids = messages.get(JournalMark.ALL_UUIDS)
        if not isinstance(uuids, list):
            uuids = messages.get(JournalMark.UUIDS)
        if not isinstance(uuids, list):
            return (), ""

        names: list[str] = []
        for uuid in uuids:
            if isinstance(uuid, str):
                names.append(uuid)

        anchor = messages.get(JournalMark.ANCHOR)
        if not isinstance(anchor, str):
            anchor = ""

        return tuple(names), anchor

    def _stranded(self, own: Sequence[Row], known: set[str]) -> Iterator[Row]:
        """Строки, чей родитель так и не встал в список (цикл parentUuid): остаются в порядке файла."""
        for row in own:
            if row.uuid not in known:
                yield row

    def _place(self, row: Row, placed: list[Row], known: set[str], waiting: dict[str, list[Row]]) -> None:
        placed.append(row)
        known.add(row.uuid)

        for child in waiting.pop(row.uuid, []):
            self._place(child, placed, known, waiting)

    def _in_transcript(self, row: Row) -> bool:
        if row.kind not in tuple(RecordType):
            return False

        return RecordType(row.kind).in_transcript()
