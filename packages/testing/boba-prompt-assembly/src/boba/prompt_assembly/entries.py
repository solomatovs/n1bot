"""Внутренний список сообщений сборки запроса: модели записей и блоки содержимого.

Записи журнала (Row) превращаются в типизированные записи списка клиента —
user, assistant, attachment, system и создаваемое нормализатором сообщение
role:"system" (api_system). Здесь же режимы сборки (WireShape), момент
запроса, итог сборки и предикаты над блоками. Модуль ничего не читает и
ошибок не выпускает: AssembleError объявлена здесь как общая ошибка слоя
сборки messages и поднимается модулями attachments, cache и messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable, Iterator, Mapping, Sequence

from boba.prompt_assembly.records import BlockType, JsText, Json, Key



Block = dict[str, Json]


Content = str | list[Block]


MarkPath = tuple[str | int, ...]


class AssembleError(Exception):
    """Единственная ошибка инструмента, выходящая наружу."""


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class Marker(StrEnum):
    """Строковые константы клиента, попадающие в сообщения."""

    HEX_DIGITS = "0123456789abcdef"
    REMINDER_OPEN = "<system-reminder>"
    REMINDER_CLOSE = "</system-reminder>"
    REMINDER_TAG = "<system-reminder"
    KEYED_OPEN = '<system-reminder id="'
    KEYED_CLOSE = '</system-reminder id="'
    KEYED_END = '">'
    NO_CONTENT = "(no content)"
    EMPTY_TEXT_REMOVED = "[Empty text removed]"
    NO_MESSAGE_CONTENT = "[No message content]"
    TOOL_LOADED = "Tool loaded."
    TOOL_USE_REMOVED = "[Tool use removed]"
    TOOL_USE_INTERRUPTED = "[Tool use interrupted]"
    ORPHANED_RESULT = "[Orphaned tool result removed due to conversation resume]"
    RESULT_MISSING = "[Tool result missing due to internal error]"
    ADVISOR_RESPONSE = "[Advisor response]"
    REFERENCES_REMOVED = "[Tool references removed - tool search not enabled]"
    ATTRIBUTION_ON = "Attribution for git commits and pull requests you create from here on"
    ATTRIBUTION_OFF = (
        "From here on, do not add attribution lines to git commit messages or pull request descriptions"
    )
    NO_RESPONSE_REQUESTED = "No response requested."
    ADVISOR_TOOL = "advisor"
    TOOL_SEARCH = "ToolSearch"
    NEXT_USER_MESSAGE = "next_user_message"
    TOOLS_SURFACED = "The following tools just became available and are ready to use:"
    TOOLS_REPLACED = "The following tools have updated definitions, which replace the earlier ones from here on:"
    TOOLS_ADDED = "The following tools are now available:"
    TOOLS_DEFERRED = (
        "The following deferred tools are now available via ToolSearch. Their schemas are NOT loaded \u2014 "
        "calling them directly will fail with InputValidationError. Use ToolSearch with query "
        '"select:<name>[,<name>...]" to load tool schemas before calling them:'
    )
    MID_TURN_HEAD = "The user sent a new message while you were working:\n"
    MID_TURN_TAIL = (
        "\n\nThis is how Claude Code surfaces messages the user sends mid-turn \u2014 within the running turn, "
        "often alongside the next tool result, rather than as a separate conversation turn. "
        "Address the message above as you continue this turn."
    )


@dataclass(frozen=True)
class UserEntry:
    """Запись user внутреннего списка либо сообщение, собранное нормализатором."""

    uuid: str
    content: Content
    is_meta: bool = False
    is_virtual: bool = False
    ephemeral: bool = False
    has_task_delivery: bool = False
    origin_kind: str = ""
    interrupted_by_shutdown: bool = False


@dataclass(frozen=True)
class AssistantEntry:
    """Запись assistant: один блок ответа либо склейка записей одного message.id."""

    uuid: str
    message_id: str | None
    model: str
    content: list[Block]
    is_virtual: bool = False
    is_api_error: bool = False
    effort: str | None = None
    per_turn_effort: str | None = None
    api_block_index: int | None = None
    api_block_indices: tuple[int, ...] = ()
    wire_tool_inputs: Mapping[str, Json] = field(default_factory=lambda: dict[str, Json]())


@dataclass(frozen=True)
class AttachmentEntry:
    """Запись attachment: тип, данные и текст, которым вложение ушло модели."""

    uuid: str
    kind: str
    attachment: Mapping[str, Json]
    rendered: Sequence[Content]
    has_rendered: bool
    rendered_role: str
    placement: str = ""


@dataclass(frozen=True)
class SystemEntry:
    """Запись system: в запрос попадает только subtype local_command."""

    uuid: str
    subtype: str
    content: str
    preserved: tuple[str, ...] = ()
    anchor: str = ""


@dataclass(frozen=True)
class ApiSystemEntry:
    """Сообщение role:"system" посреди диалога (api_system), создаётся нормализатором."""

    content: str
    ephemeral: bool = False
    ephemeral_suffix: str = ""
    tool_additions: tuple[str, ...] = ()
    tool_removals: tuple[str, ...] = ()
    output_config: Mapping[str, Json] | None = None
    name_only_content: str | None = None
    from_records: bool = False


Entry = UserEntry | AssistantEntry | AttachmentEntry | SystemEntry | ApiSystemEntry


@dataclass(frozen=True)
class HistoryRecord:
    """Запись истории с положением в файле и ссылкой на родителя."""

    line: int
    uuid: str
    parent: str
    entry: Entry


@dataclass(frozen=True)
class WireShape:
    """Режимы сборки запроса; берутся из вложений prompt_snapshot и записей истории.

    system_turns — режим mid_conv_system (X8), tool_change_header — блоки
    tool_addition (surfaceLateToolAdditions), wrap_system_text — модель
    claude-sonnet-5 (anr): текст системного сообщения остаётся в обёртке,
    kept_reminders — напоминания со сроком уходят отдельным сообщением с
    clear_at, name_only_announcements — uuid вложений deferred_tools_delta,
    объявленных только именами (без блоков tool_addition).
    """

    system_turns: bool
    tool_change_header: bool
    reminder_fold: bool
    echo_wire_tool_inputs: bool
    tool_search: bool
    advisor: bool
    per_turn_effort: bool
    echo_api_block_order: bool
    wrap_system_text: bool
    prompt_caching: bool
    kept_reminders: bool
    deferred_wire_tools: frozenset[str] | None
    name_only_announcements: frozenset[str]


@dataclass(frozen=True)
class Moment:
    """Момент запроса: строка, перед которой лежит его история, и усилие этого запроса."""

    ordinal: int
    line: int
    effort: str | None


@dataclass(frozen=True)
class Assembled:
    """Итог сборки одного запроса: сообщения провода и пути меток кэша."""

    ordinal: int
    line: int
    messages: list[dict[str, Json]]
    marks: list[MarkPath]
    notes: list[str]


class BlockKit:
    """Предикаты и мелкие преобразования блоков контента."""

    def __init__(self) -> None:
        self._js = JsText()

    def some(self, blocks: Sequence[Block], predicate: Callable[[Block], bool]) -> bool:
        """Хотя бы один блок удовлетворяет предикату."""
        for block in blocks:
            if predicate(block):
                return True

        return False

    def every(self, blocks: Sequence[Block], predicate: Callable[[Block], bool]) -> bool:
        """Все блоки удовлетворяют предикату (пустой список — да)."""
        for block in blocks:
            if not predicate(block):
                return False

        return True

    def kind(self, block: Block) -> str:
        value = block.get(Key.TYPE)
        if isinstance(value, str):
            return value

        return ""

    def text(self, block: Block) -> str:
        value = block.get(Key.TEXT)
        if isinstance(value, str):
            return value

        return ""

    def text_block(self, text: str) -> Block:
        return {Key.TYPE: BlockType.TEXT, Key.TEXT: text}

    def cited_text(self, text: str) -> Block:
        return {Key.TYPE: BlockType.TEXT, Key.TEXT: text, Key.CITATIONS: []}

    def as_blocks(self, content: Content) -> list[Block]:
        if isinstance(content, str):
            return [self.text_block(content)]

        return content

    def is_text(self, block: Block) -> bool:
        return self.kind(block) == BlockType.TEXT

    def is_thinking(self, block: Block) -> bool:
        return self.kind(block) in (BlockType.THINKING, BlockType.REDACTED_THINKING)

    def is_tool_use(self, block: Block) -> bool:
        return self.kind(block) == BlockType.TOOL_USE

    def is_tool_result(self, block: Block) -> bool:
        return self.kind(block) == BlockType.TOOL_RESULT

    def is_empty_text(self, block: Block) -> bool:
        if not self.is_text(block):
            return False

        return block.get(Key.TEXT) == ""

    def has_reference(self, content: Json) -> bool:
        if not isinstance(content, list):
            return False

        for item in content:
            if isinstance(item, dict) and item.get(Key.TYPE) == BlockType.TOOL_REFERENCE:
                return True

        return False

    def id_of(self, block: Block, key: str) -> str:
        value = block.get(key)
        if isinstance(value, str):
            return value

        return ""

    def whitespace_only(self, content: Sequence[Block]) -> bool:
        """sye: до первого текста — только мышление, тексты пустые либо заглушки."""
        seen_text = False

        for block in content:
            if not seen_text and self.is_thinking(block):
                continue
            if not self.is_text(block):
                return False
            trimmed = self._js.trim(self.text(block))
            if trimmed not in ("", Marker.NO_CONTENT, Marker.EMPTY_TEXT_REMOVED):
                return False
            seen_text = True

        return seen_text

    def substantive(self, block: Block) -> bool:
        """jCr: блок несёт содержание (не мышление и не пустой текст)."""
        if self.is_thinking(block):
            return False

        if not self.is_text(block):
            return True

        trimmed = self._js.trim(self.text(block))
        return trimmed not in ("", Marker.NO_CONTENT, Marker.EMPTY_TEXT_REMOVED)

    def tool_results_first(self, blocks: Sequence[Block]) -> list[Block]:
        results: list[Block] = []
        others: list[Block] = []

        for block in blocks:
            if self.is_tool_result(block):
                results.append(block)
                continue
            others.append(block)

        return [*results, *others]

    def tool_uses_last(self, blocks: list[Block]) -> list[Block]:
        """c_n: tool_use уходят в конец, если между ними нет чередования с мышлением."""
        first = -1
        other_after = False
        interleaved = False
        previous_thinking = False
        tool_since = False

        for position, block in enumerate(blocks):
            if self.is_tool_use(block):
                if first == -1:
                    first = position
                tool_since = True
                continue

            if first != -1:
                other_after = True

            thinking = self.is_thinking(block)
            if thinking and previous_thinking and tool_since:
                interleaved = True

            previous_thinking = thinking
            tool_since = False

        if not other_after:
            return blocks

        if interleaved:
            return blocks

        return [*self._without_tool_uses(blocks), *self._tool_uses(blocks)]

    def _tool_uses(self, blocks: Sequence[Block]) -> Iterator[Block]:
        for block in blocks:
            if self.is_tool_use(block):
                yield block

    def _without_tool_uses(self, blocks: Sequence[Block]) -> Iterator[Block]:
        for block in blocks:
            if not self.is_tool_use(block):
                yield block
