"""Сборка messages запроса из записей истории (Claude Code 2.1.289).

Повторяет конвейер клиента от списка записей в памяти до сообщений провода:
нормализация (HE @211663642), склейка user-сообщений, правила thinking, починка
пар tool_use/tool_result при сборке (Jqt @211759180), усилие хода (IGt) и
сериализация с путями меток кэша (vzo @209717364).

Ошибки:
AssembleError — запись истории не разбирается, вложение не рендерится либо
    история перед строкой не восстанавливается; текст называет журнал,
    строку и причину. Ошибки загрузчика (LoaderError) приходят упакованными
    в неё.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

from boba.prompt_assembly.attachments import DeferredToolsText, QueuedCommandRenderer, Reminders
from boba.prompt_assembly.cache import CacheMarks
from boba.prompt_assembly.entries import (
    ApiSystemEntry,
    AssembleError,
    Assembled,
    AssistantEntry,
    AttachmentEntry,
    Block,
    BlockKit,
    Content,
    Entry,
    Marker,
    MarkPath,
    Moment,
    Role,
    SystemEntry,
    UserEntry,
    WireShape,
)
from boba.prompt_assembly.loader import LoaderError, SessionMemory
from boba.prompt_assembly.records import (
    AttachmentKind,
    BlockType,
    Journal,
    JsText,
    Json,
    Key,
    RecordType,
    Row,
    Subtype,
    Synthetic,
)



class UserMerge:
    """Слияние user-сообщений (bM) и подмешивание напоминаний (byn/f_n/r6e)."""

    def __init__(self) -> None:
        self._blocks = BlockKit()
        self._reminders = Reminders()
        self._js = JsText()

    def merge(self, first: UserEntry, second: UserEntry) -> UserEntry:
        """bM: стык текст|текст получает перевод строки, tool_result идут первыми."""
        left = self._blocks.as_blocks(first.content)
        right = self._blocks.as_blocks(second.content)
        uuid = first.uuid
        if first.is_meta:
            uuid = second.uuid

        return replace(
            first,
            uuid=uuid,
            ephemeral=first.ephemeral or second.ephemeral,
            content=self._blocks.tool_results_first(self._join(left, right)),
        )

    def merge_reminder(self, first: UserEntry, second: UserEntry, fold: bool, system_turns: bool) -> UserEntry:
        """byn: напоминание вложения дописывается к предыдущему user-сообщению."""
        left = self._blocks.as_blocks(first.content)
        right = self._blocks.as_blocks(second.content)
        placed = self._place(left, right, fold, system_turns)

        return replace(
            first,
            ephemeral=first.ephemeral or second.ephemeral,
            content=self._blocks.tool_results_first(placed),
        )

    def merge_adjacent(self, entries: Sequence[Entry]) -> list[Entry]:
        """rye: подряд идущие user-сообщения сливаются в одно."""
        merged: list[Entry] = []

        for entry in entries:
            last = self._last(merged)
            if isinstance(entry, UserEntry) and isinstance(last, UserEntry):
                merged[-1] = self.merge(last, entry)
                continue
            merged.append(entry)

        return merged

    def fold_into_results(self, entries: Sequence[Entry]) -> list[Entry]:
        """a_n: в режиме reminderFold без mid_conv напоминания-соседи вклеиваются в последний tool_result."""
        folded: list[Entry] = []

        for entry in entries:
            if not isinstance(entry, UserEntry) or isinstance(entry.content, str):
                folded.append(entry)
                continue
            folded.append(replace(entry, content=self._folded(entry.content)))

        return folded

    def _folded(self, content: list[Block]) -> list[Block]:
        reminders: list[Block] = []
        rest: list[Block] = []
        for block in content:
            if self._foldable(block):
                reminders.append(block)
                continue
            rest.append(block)

        if not reminders:
            return content

        target = -1
        for position, block in enumerate(rest):
            if self._blocks.is_tool_result(block):
                target = position

        if target == -1:
            return content

        smooshed = self.smoosh(rest[target], reminders)
        if smooshed is None:
            return content

        return [*rest[:target], smooshed, *rest[target + 1 :]]

    def _foldable(self, block: Block) -> bool:
        """Текст-напоминание (обычное или keyed), кроме события и напоминания об атрибуции."""
        if not self._blocks.is_text(block):
            return False

        text = self._blocks.text(block)
        if self._reminders.attribution(text):
            return False

        if text.startswith(f"{Marker.REMINDER_OPEN}\n<event "):
            return False

        if text.startswith(Marker.REMINDER_OPEN):
            return True

        return self._reminders.keyed_id(text) is not None

    def smoosh(self, result: Block, extra: Sequence[Block]) -> Block | None:
        """r6e: текст напоминаний вклеивается в tool_result через пустую строку."""
        if not extra:
            return result

        content = result.get(Key.CONTENT)
        if self._blocks.has_reference(content):
            return None

        added = list(extra)
        if result.get(Key.IS_ERROR):
            added = list(self._texts_only(added))
            if not added:
                return result

        all_text = self._blocks.every(added, self._blocks.is_text)
        if all_text and (content is None or isinstance(content, str)):
            return {**result, Key.CONTENT: self._joined_text(content, added)}

        return {**result, Key.CONTENT: self._joined_blocks(content, added)}

    def _place(self, left: list[Block], right: list[Block], fold: bool, system_turns: bool) -> list[Block]:
        last = self._last_block(left)
        if last is None or not self._blocks.is_tool_result(last):
            return self._place_after_siblings(left, right, fold, system_turns)

        if self._any_sibling(right, system_turns):
            return [*left, *right]

        if not fold:
            return self._place_unfolded(left, last, right)

        extra = [block for block in right if not self._blocks.is_tool_result(block)]
        results = [block for block in right if self._blocks.is_tool_result(block)]
        if not extra:
            return [*left, *right]

        folded = self.smoosh(last, extra)
        if folded is None:
            return [*left, *right]

        return [*left[:-1], folded, *results]

    def _place_after_siblings(
        self, left: list[Block], right: list[Block], fold: bool, system_turns: bool
    ) -> list[Block]:
        cut = self._sibling_tail_start(left, system_turns)
        if cut >= len(left):
            return [*left, *right]

        if cut == 0:
            return [*left, *right]

        if not self._blocks.is_tool_result(left[cut - 1]):
            return [*left, *right]

        if self._any_sibling(right, system_turns):
            return [*left, *right]

        return [*self._place(left[:cut], right, fold, system_turns), *left[cut:]]

    def _place_unfolded(self, left: list[Block], last: Block, right: list[Block]) -> list[Block]:
        if not self._blocks.every(right, self._blocks.is_text):
            return [*left, *right]

        content = last.get(Key.CONTENT)
        if content is None or isinstance(content, str):
            smooshed = self.smoosh(last, right)
            if smooshed is None:
                return [*left, *right]
            return [*left[:-1], smooshed]

        if not isinstance(content, list):
            return [*left, *right]

        if self._blocks.has_reference(content):
            return [*left, *right]

        inner = list(content)
        tail = self._last_json_block(inner)
        if tail is not None and self._blocks.is_text(tail):
            inner[-1] = {**tail, Key.TEXT: f"{self._blocks.text(tail)}\n"}

        return [*left[:-1], {**last, Key.CONTENT: [*inner, *right]}]

    def _sibling_tail_start(self, blocks: Sequence[Block], system_turns: bool) -> int:
        position = len(blocks)

        while position > 0:
            block = blocks[position - 1]
            if not self._blocks.is_text(block):
                break
            if not self._sibling(self._blocks.text(block), system_turns):
                break
            position -= 1

        return position

    def _any_sibling(self, blocks: Sequence[Block], system_turns: bool) -> bool:
        for block in blocks:
            if not self._blocks.is_text(block):
                continue
            if self._sibling(self._blocks.text(block), system_turns):
                return True

        return False

    def _sibling(self, text: str, system_turns: bool) -> bool:
        """Q4e: напоминание об атрибуции и keyed-напоминание остаются соседним блоком."""
        if self._reminders.attribution(text):
            return True

        if not system_turns:
            return False

        return self._reminders.keyed_id(text) is not None

    def _join(self, left: list[Block], right: list[Block]) -> list[Block]:
        """aCr."""
        last = self._last_block(left)
        first = self._first_block(right)
        if last is None or first is None:
            return [*left, *right]

        if not self._blocks.is_text(last):
            return [*left, *right]

        if not self._blocks.is_text(first):
            return [*left, *right]

        return [*left[:-1], {**last, Key.TEXT: f"{self._blocks.text(last)}\n"}, *right]

    def _joined_text(self, content: Json, added: Sequence[Block]) -> str:
        parts: list[str] = []
        if isinstance(content, str):
            parts.append(self._js.trim(content))

        for block in added:
            parts.append(self._js.trim(self._blocks.text(block)))

        filled: list[str] = []
        for part in parts:
            if part:
                filled.append(part)

        return "\n\n".join(filled)

    def _joined_blocks(self, content: Json, added: Sequence[Block]) -> list[Json]:
        source: list[Json] = []
        if isinstance(content, str) and self._js.trim(content):
            source.append(self._blocks.text_block(self._js.trim(content)))
        if isinstance(content, list):
            source.extend(content)
        source.extend(added)

        joined: list[Json] = []
        for item in source:
            if not isinstance(item, dict) or not self._blocks.is_text(item):
                joined.append(item)
                continue
            trimmed = self._js.trim(self._blocks.text(item))
            if not trimmed:
                continue
            previous = self._last_json_block(joined)
            if previous is not None and self._blocks.is_text(previous):
                joined[-1] = {**previous, Key.TEXT: f"{self._blocks.text(previous)}\n\n{trimmed}"}
                continue
            joined.append(self._blocks.text_block(trimmed))

        return joined

    def _texts_only(self, blocks: Sequence[Block]) -> Iterator[Block]:
        for block in blocks:
            if self._blocks.is_text(block):
                yield block

    def _last(self, entries: Sequence[Entry]) -> Entry | None:
        if not entries:
            return None

        return entries[-1]

    def _last_block(self, blocks: Sequence[Block]) -> Block | None:
        if not blocks:
            return None

        return blocks[-1]

    def _first_block(self, blocks: Sequence[Block]) -> Block | None:
        if not blocks:
            return None

        return blocks[0]

    def _last_json_block(self, items: Sequence[Json]) -> Block | None:
        if not items:
            return None

        last = items[-1]
        if isinstance(last, dict):
            return last

        return None


class ThinkingRules:
    """Правила про мышление и пустые ответы ассистента (A_n, x_n, zCr, E_n, qCr)."""

    def __init__(self) -> None:
        self._blocks = BlockKit()
        self._merge = UserMerge()

    def promote_strings(self, entries: Sequence[Entry]) -> list[Entry]:
        """WCr = pre(GCr): до неподвижной точки убирает осиротевшее мышление и пустые ответы."""
        current = list(entries)

        while True:
            reduced = self.drop_blank_assistants(self.drop_orphan_thinking(current), False)
            if len(reduced) == len(current):
                return current
            current = reduced

    def drop_orphan_thinking(self, entries: Sequence[Entry]) -> list[Entry]:
        """A_n: ответ только из мышления без соседа с тем же message.id не отправляется."""
        with_content = set(self._ids_with(entries, self._not_thinking))
        kept: list[Entry] = []

        for entry in entries:
            if not isinstance(entry, AssistantEntry):
                kept.append(entry)
                continue
            if not self._thinking_only(entry):
                kept.append(entry)
                continue
            if entry.message_id is not None and entry.message_id in with_content:
                kept.append(entry)

        return kept

    def drop_blank_assistants(self, entries: Sequence[Entry], merge_users: bool) -> list[Entry]:
        """x_n: ответ из пробельного текста без содержательного соседа удаляется."""
        if not self._some_entry(entries, self._blank_assistant):
            return list(entries)

        substantive = set(self._ids_with(entries, self._blocks.substantive))
        kept: list[Entry] = []

        for entry in entries:
            if not isinstance(entry, AssistantEntry):
                kept.append(entry)
                continue
            if entry.message_id is not None and entry.message_id in substantive:
                kept.append(entry)
                continue
            if not self._blank_assistant(entry):
                kept.append(entry)

        if not merge_users:
            return kept

        merged: list[Entry] = []
        for entry in kept:
            last = self._last(merged)
            if not isinstance(entry, UserEntry) or not isinstance(last, UserEntry):
                merged.append(entry)
                continue
            if entry.interrupted_by_shutdown or last.interrupted_by_shutdown:
                merged.append(entry)
                continue
            merged[-1] = self._merge.merge(last, entry)

        return merged

    def drop_empty_texts(self, entries: Sequence[Entry]) -> list[Entry]:
        """zCr: пустые тексты удаляются; между двумя блоками мышления — заглушка."""
        if not self._some_entry(entries, self._has_empty_text):
            return list(entries)

        counts: dict[str, int] = {}
        for entry in entries:
            if isinstance(entry, AssistantEntry) and entry.message_id is not None:
                counts[entry.message_id] = counts.get(entry.message_id, 0) + 1

        keeps = set(self._ids_with(entries, self._neither_thinking_nor_empty))
        result: list[Entry] = []

        for entry in entries:
            if not isinstance(entry, AssistantEntry) or not self._has_empty_text(entry):
                result.append(entry)
                continue
            alone = entry.message_id is None or counts.get(entry.message_id) == 1
            if self._blocks.whitespace_only(entry.content) and alone:
                result.append(entry)
                continue
            anchored = self._blocks.some(entry.content, self._neither_thinking_nor_empty)
            if entry.message_id is not None and entry.message_id in keeps:
                anchored = True
            content = list(self._without_empty_texts(entry.content, anchored))
            if content:
                result.append(replace(entry, content=content))

        if len(result) == len(entries):
            return result

        return self._merge.merge_adjacent(result)

    def strip_trailing(self, entries: Sequence[Entry]) -> list[Entry]:
        """E_n: хвостовое мышление последнего ответа ассистента срезается."""
        result = list(entries)
        last = self._last(result)
        if not isinstance(last, AssistantEntry):
            return result

        content = last.content
        if not content or not self._blocks.is_thinking(content[-1]):
            return result

        keep = len(content)
        while keep > 0 and self._blocks.is_thinking(content[keep - 1]):
            keep -= 1

        trimmed = content[:keep]
        if keep == 0:
            trimmed = [self._blocks.cited_text(Marker.NO_MESSAGE_CONTENT)]

        result[-1] = replace(last, content=trimmed)
        return result

    def trim_tail(self, entries: Sequence[Entry]) -> list[Entry]:
        """w0e: у последнего ответа срезаются хвостовые пробельные тексты (fRr) и мышление (E_n)."""
        current = list(entries)

        while True:
            trimmed = self.strip_trailing(self._without_blank_tail(current))
            if self._identical(trimmed, current):
                return current
            current = trimmed

    def _without_blank_tail(self, entries: list[Entry]) -> list[Entry]:
        last = self._last(entries)
        if not isinstance(last, AssistantEntry):
            return entries

        keep = len(last.content)
        while keep > 0 and self._blank_text(last.content[keep - 1]):
            keep -= 1

        if keep == len(last.content):
            return entries

        if keep == 0:
            return entries[:-1]

        return [*entries[:-1], replace(last, content=last.content[:keep], api_block_indices=())]

    def _blank_text(self, block: Block) -> bool:
        if not self._blocks.is_text(block):
            return False

        text = self._blocks.text(block)
        return text != "" and text.strip() == ""

    def fill_empty(self, entries: Sequence[Entry]) -> list[Entry]:
        """qCr: пустой content не у последнего ассистента получает заглушку."""
        result = list(entries)

        for position in range(len(result) - 1):
            entry = result[position]
            if not isinstance(entry, AssistantEntry) or entry.content:
                continue
            result[position] = replace(entry, content=[self._blocks.cited_text(Marker.NO_CONTENT)])

        return result

    def _without_empty_texts(self, content: Sequence[Block], anchored: bool) -> Iterator[Block]:
        for position, block in enumerate(content):
            if not self._blocks.is_empty_text(block):
                yield block
                continue
            if not anchored or position == 0:
                continue
            following = self._next_non_empty(content, position)
            if following is None:
                continue
            if self._blocks.is_thinking(content[position - 1]) and self._blocks.is_thinking(following):
                yield self._blocks.cited_text(Marker.EMPTY_TEXT_REMOVED)

    def _next_non_empty(self, content: Sequence[Block], position: int) -> Block | None:
        for block in content[position + 1 :]:
            if not self._blocks.is_empty_text(block):
                return block

        return None

    def _ids_with(self, entries: Sequence[Entry], predicate: Callable[[Block], bool]) -> Iterator[str]:
        for entry in entries:
            if not isinstance(entry, AssistantEntry) or not entry.message_id:
                continue
            if self._blocks.some(entry.content, predicate):
                yield entry.message_id

    def _not_thinking(self, block: Block) -> bool:
        return not self._blocks.is_thinking(block)

    def _neither_thinking_nor_empty(self, block: Block) -> bool:
        if self._blocks.is_thinking(block):
            return False

        return not self._blocks.is_empty_text(block)

    def _thinking_only(self, entry: AssistantEntry) -> bool:
        if not entry.content:
            return False

        return self._blocks.every(entry.content, self._blocks.is_thinking)

    def _blank_assistant(self, entry: Entry) -> bool:
        if not isinstance(entry, AssistantEntry) or not entry.content:
            return False

        return self._blocks.whitespace_only(entry.content)

    def _has_empty_text(self, entry: Entry) -> bool:
        if not isinstance(entry, AssistantEntry):
            return False

        return self._blocks.some(entry.content, self._blocks.is_empty_text)

    def _some_entry(self, entries: Sequence[Entry], predicate: Callable[[Entry], bool]) -> bool:
        for entry in entries:
            if predicate(entry):
                return True

        return False

    def _identical(self, left: Sequence[Entry], right: Sequence[Entry]) -> bool:
        """Списки состоят из одних и тех же объектов в том же порядке."""
        if len(left) != len(right):
            return False

        for one, two in zip(left, right):
            if one is not two:
                return False

        return True

    def _last(self, entries: Sequence[Entry]) -> Entry | None:
        if not entries:
            return None

        return entries[-1]


@dataclass
class _Feed:
    """Состояние одного прохода нормализатора: выход и накопленное для api_system."""

    out: list[Entry] = field(default_factory=lambda: list[Entry]())
    texts: list[str] = field(default_factory=lambda: list[str]())
    plain_texts: list[str] = field(default_factory=lambda: list[str]())
    ephemerals: list[str] = field(default_factory=lambda: list[str]())
    kept: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    additions: list[str] = field(default_factory=lambda: list[str]())
    tail: bool = False
    created_system: bool = False
    index: dict[str | None, int] = field(default_factory=lambda: dict[str | None, int]())
    cursor: int = 0
    notes: list[str] = field(default_factory=lambda: list[str]())


class Normalizer:
    """Главный нормализатор HE: внутренний список → сообщения user/assistant/api_system.

    Вызывается Assembler'ом на срезе истории; пользуется UserMerge для слияния
    сообщений и ThinkingRules для правил про мышление. Текст вложений берёт из
    поля rendered записи.
    """

    _NEVER_SENT = frozenset(
        ("thinking_drop", "credential_org", "prompt_snapshot", "prompt_render_point", "deferred_tools_record")
    )
    _USER_ROLE = frozenset(
        (
            "dir_sync_notice",
            "unknown_command_fallback",
            "session_context",
            "instructions",
            "coordinator_context",
            "context_sections",
            "remote_session_change",
            "fork_briefing",
            "poll_events",
            "cowork_memory_context",
            "account_memory_recall",
            "artifact_opening_prefetch",
        )
    )
    _CLOSING_LINE = frozenset(
        (
            "sandbox_instructions",
            "environment",
            "model",
            "proactivity",
            "attention_budget",
            "deferred_tools_delta",
            "agent_listing_delta",
            "mcp_instructions_delta",
            "mcp_dropped_tools_delta",
            "skill_listing",
            "output_style_instructions",
            "language",
            "auto_mode",
        )
    )
    _UNMODELLED = frozenset(
        (
            AttachmentKind.POLL_EVENTS,
            AttachmentKind.BATCHING_REMINDER,
            AttachmentKind.SECONDARY_REMINDER,
        )
    )
    _SENT_REMINDERS = (AttachmentKind.BATCHING_REMINDER_SENT, AttachmentKind.SECONDARY_REMINDER_SENT)

    def __init__(self, shape: WireShape) -> None:
        self._shape = shape
        self._blocks = BlockKit()
        self._reminders = Reminders()
        self._merge = UserMerge()
        self._thinking = ThinkingRules()
        self._queued = QueuedCommandRenderer()
        self._deferred = DeferredToolsText()
        self._js = JsText()

    def run(self, history: Sequence[Entry], notes: list[str]) -> list[Entry]:
        prepared = self._thinking.promote_strings(history)
        ordered = self.reorder(prepared)
        feed = _Feed(notes=notes)
        tail_start = self._tail_start(ordered)

        for position, entry in enumerate(ordered):
            if self.never_sent(entry):
                continue
            feed.tail = position >= tail_start
            self._take(entry, feed)

        self._flush(feed)
        return self._finish(feed)

    def never_sent(self, entry: Entry) -> bool:
        """ny: записи, которые не уходят в запрос ни при каких условиях."""
        if isinstance(entry, ApiSystemEntry):
            return False

        if isinstance(entry, SystemEntry):
            return entry.subtype != Subtype.LOCAL_COMMAND

        if isinstance(entry, AttachmentEntry):
            return entry.kind in self._NEVER_SENT

        if entry.is_virtual:
            return True

        if isinstance(entry, UserEntry):
            return False

        return entry.is_api_error and entry.model == Synthetic.MODEL

    def reorder(self, entries: Sequence[Entry]) -> list[Entry]:
        """FRr: вложения всплывают вверх до ассистента либо user с tool_result."""
        moving = False
        for entry in entries:
            if self._moves(entry):
                moving = True
                break

        if not moving:
            return list(entries)

        result: list[Entry] = []
        pending: list[AttachmentEntry] = []

        for entry in reversed(entries):
            if isinstance(entry, AttachmentEntry):
                pending.append(entry)
                continue

            virtual = self._virtual(entry)
            if not self._stops(entry) or not pending:
                if not virtual:
                    result.append(entry)
                continue

            above: list[AttachmentEntry] = []
            for attachment in pending:
                if self._stays_above(attachment, entry):
                    above.append(attachment)
                    continue
                result.append(attachment)

            if not virtual:
                result.append(entry)

            pending = above

        result.extend(pending)
        result.reverse()
        return result

    def _take(self, entry: Entry, feed: _Feed) -> None:
        if isinstance(entry, SystemEntry):
            self._take_local_command(entry, feed)
            return

        if isinstance(entry, UserEntry):
            self._take_user(entry, feed)
            return

        if isinstance(entry, AssistantEntry):
            self._take_assistant(entry, feed)
            return

        if isinstance(entry, AttachmentEntry):
            self._take_attachment(entry, feed)

    def _take_local_command(self, entry: SystemEntry, feed: _Feed) -> None:
        feed.kept.clear()
        message = UserEntry(uuid=entry.uuid, content=entry.content)
        self._push_user(message, feed)

    def _take_user(self, entry: UserEntry, feed: _Feed) -> None:
        if isinstance(entry.content, list) and not entry.content:
            return

        feed.kept.clear()
        if entry.has_task_delivery:
            feed.notes.append(f"user {entry.uuid}: taskDelivery is not modelled, sent as a plain message")

        if entry.origin_kind == "task-notification":
            feed.notes.append(f"user {entry.uuid}: task-notification wrapping (cKr) is not modelled")

        message = entry
        if not self._shape.tool_search:
            message = self._strip_references(message)

        message = self._mark_tool_loaded(message)
        message = self._escape(message)
        self._push_user(message, feed)

    def _take_assistant(self, entry: AssistantEntry, feed: _Feed) -> None:
        message = self._wire_tool_uses(entry)

        while feed.cursor < len(feed.out):
            seen = feed.out[feed.cursor]
            if isinstance(seen, AssistantEntry):
                feed.index[seen.message_id] = feed.cursor
            if isinstance(seen, UserEntry) and not self._has_tool_result(seen):
                feed.index.clear()
            if isinstance(seen, (AttachmentEntry, SystemEntry)):
                feed.index.clear()
            feed.cursor += 1

        position = feed.index.get(message.message_id)
        if position is not None:
            target = feed.out[position]
            if isinstance(target, AssistantEntry):
                feed.out[position] = self._merge_assistant(target, message)
                return

        self._flush(feed)
        ordered = message.content
        if not self._keeps_api_order(message):
            ordered = self._blocks.tool_uses_last(message.content)

        if ordered is message.content:
            feed.out.append(message)
            return

        feed.out.append(replace(message, content=ordered, api_block_index=None, api_block_indices=()))

    def _take_attachment(self, entry: AttachmentEntry, feed: _Feed) -> None:
        if entry.kind in self._UNMODELLED:
            feed.notes.append(f"attachment {entry.uuid}: type {entry.kind} is not modelled, skipped")
            return

        if entry.kind in self._SENT_REMINDERS:
            self._take_sent_reminder(entry, feed)
            return

        if entry.attachment.get("humanTurn") is True:
            feed.notes.append(f"attachment {entry.uuid}: queued_command in a human turn is not modelled")

        additions = self._surfaced(entry)
        rendered = self._rendered(entry, additions)
        if rendered is None:
            feed.notes.append(
                f"attachment {entry.uuid}: type {entry.kind} has no rendered text, treated as rendering to nothing"
            )
            return

        messages = self._reminder_messages(entry, rendered)

        if self._shape.system_turns and self._promoted(entry):
            text = self._system_text(messages)
            if text is not None:
                feed.texts.append(self._system_wrapped(text))
                feed.plain_texts.append(self._system_wrapped(self._name_only_text(entry, additions, text)))
                feed.additions.extend(additions)
                return

        if self._shape.reminder_fold:
            messages = [self._wrap_unwrapped(message) for message in messages]

        last = self._last(feed.out)
        if not isinstance(last, UserEntry):
            feed.out.extend(messages)
            return

        merged = last
        for message in messages:
            merged = self._merge.merge_reminder(
                merged, message, self._shape.reminder_fold, self._shape.system_turns
            )

        feed.out[-1] = merged

    def _take_sent_reminder(self, entry: AttachmentEntry, feed: _Feed) -> None:
        """Записи batching_reminder_sent / secondary_reminder_sent: текст напоминания со сроком.

        В памяти клиента текущее напоминание — несохраняемое вложение
        batching_reminder; в журнале ему соответствует запись *_sent в хвосте.
        """
        if not self._shape.system_turns:
            return

        text = entry.attachment.get(Key.TEXT)
        if not isinstance(text, str) or self._js.blank(text):
            text = ""

        scoped = entry.attachment.get("clearAt") == Marker.NEXT_USER_MESSAGE
        if self._shape.kept_reminders and scoped:
            feed.kept[entry.kind] = self._system_wrapped(text)
            return

        if not feed.tail or text == "":
            return

        feed.notes.append(f"attachment {entry.uuid}: {entry.kind} without kept reminders is taken as the current one")
        feed.ephemerals.append(self._system_wrapped(text))

    def _reminder_messages(self, entry: AttachmentEntry, rendered: Sequence[Content]) -> list[UserEntry]:
        messages: list[UserEntry] = []
        for content in rendered:
            messages.append(self._escape(UserEntry(uuid=entry.uuid, content=content, is_meta=True)))

        return messages

    def _rendered(self, entry: AttachmentEntry, additions: Sequence[str]) -> Sequence[Content] | None:
        """apt: текст из поля rendered; повторный рендер — когда его нет или набор всплывших имён другой."""
        if entry.kind == AttachmentKind.DEFERRED_TOOLS_DELTA and entry.has_rendered:
            return self._delta_rendered(entry, additions)

        if entry.has_rendered:
            return entry.rendered

        if entry.kind != AttachmentKind.QUEUED_COMMAND:
            return None

        return self._queued.render(entry.attachment)

    def _delta_rendered(self, entry: AttachmentEntry, additions: Sequence[str]) -> Sequence[Content]:
        """$Cr: записанный текст годится, только если набор всплывших имён тот же."""
        recorded = self._deferred.recorded_names(entry.attachment)
        if len(recorded) == len(additions) and set(recorded) == set(additions):
            return entry.rendered

        rerendered = self._deferred.rerender(entry.attachment, entry.rendered, additions)
        if rerendered is None:
            return entry.rendered

        return rerendered

    def _name_only_text(self, entry: AttachmentEntry, additions: Sequence[str], text: str) -> str:
        """Текст вложения без раздела о всплывших инструментах (qn): им понижается api_system."""
        if not additions:
            return text

        rerendered = self._deferred.rerender(entry.attachment, entry.rendered, [])
        if rerendered is None:
            return text

        plain = self._system_text(self._reminder_messages(entry, rerendered))
        if plain is None:
            return text

        return plain

    def _system_wrapped(self, text: str) -> str:
        if not self._shape.wrap_system_text or text == "":
            return text

        return self._reminders.wrap(text)

    def _tail_start(self, ordered: Sequence[Entry]) -> int:
        position = len(ordered)

        while position > 0 and isinstance(ordered[position - 1], AttachmentEntry):
            position -= 1

        return position

    def _flush(self, feed: _Feed) -> None:
        """Wo: накопленные системные тексты становятся api_system либо user-напоминанием."""
        kept: list[str] = []
        for kind in self._SENT_REMINDERS:
            reminder = feed.kept.get(kind, "")
            if not self._js.blank(reminder):
                kept.append(reminder)

        feed.kept.clear()
        if not feed.texts and not feed.ephemerals and not kept:
            return

        text = "\n\n".join(feed.texts)
        transient = "\n\n".join(feed.ephemerals)
        ephemeral = "\n\n".join([*feed.ephemerals, *kept])
        additions = tuple(feed.additions)
        name_only: str | None = None
        if additions:
            name_only = "\n\n".join(feed.plain_texts)

        feed.texts.clear()
        feed.plain_texts.clear()
        feed.ephemerals.clear()
        feed.additions.clear()
        last = self._last(feed.out)

        if isinstance(last, ApiSystemEntry):
            extended = self._extend_system(last, text, ephemeral, name_only, additions)
            feed.out[-1] = replace(extended, from_records=last.from_records or bool(kept))
            return

        if isinstance(last, UserEntry):
            feed.created_system = True
            created = self._extend_system(ApiSystemEntry(content=""), text, ephemeral, name_only, additions)
            feed.out.append(replace(created, from_records=bool(kept)))
            return

        body = text
        if name_only is not None:
            body = name_only

        body = self._pair(body, transient)
        if kept and body == "":
            return

        content = body
        if not self._shape.wrap_system_text:
            content = self._reminders.wrap(body)

        feed.out.append(UserEntry(uuid="", content=content, is_meta=True, ephemeral=transient != ""))

    def _finish(self, feed: _Feed) -> list[Entry]:
        entries = self._thinking.drop_empty_texts(feed.out)
        kept = self._thinking.drop_orphan_thinking(entries)
        if len(kept) != len(entries):
            kept = self._merge.merge_adjacent(kept)

        entries = self._thinking.strip_trailing(kept)
        entries = self._thinking.drop_blank_assistants(entries, True)
        entries = self._thinking.fill_empty(entries)

        if self._shape.system_turns and feed.created_system:
            entries = self._settle_systems(entries)

        if not self._shape.system_turns and self._shape.reminder_fold:
            entries = self._merge.fold_into_results(self._merge.merge_adjacent(entries))

        if self._shape.echo_api_block_order:
            entries = self._thinking.trim_tail(entries)

        return self._text_only_errors(entries)

    def _settle_systems(self, entries: Sequence[Entry]) -> list[Entry]:
        """sCr: api_system допустим только после user и перед ассистентом, api_system или концом."""
        result: list[Entry] = []
        changed = False

        for position, entry in enumerate(entries):
            if not isinstance(entry, ApiSystemEntry):
                result.append(entry)
                continue

            previous = self._last(result)
            if isinstance(previous, ApiSystemEntry):
                changed = True
                result[-1] = self._absorb_system(previous, entry)
                continue

            following: Entry | None = None
            if position + 1 < len(entries):
                following = entries[position + 1]

            after_user = isinstance(previous, UserEntry)
            before_ok = following is None or isinstance(following, (AssistantEntry, ApiSystemEntry))
            if after_user and before_ok:
                result.append(entry)
                continue

            changed = True
            demoted = self._demoted(entry)
            if demoted is not None:
                result.append(demoted)

        if not changed:
            return result

        return self._merge.merge_adjacent(result)

    def _demoted(self, entry: ApiSystemEntry) -> UserEntry | None:
        """Понижение api_system до user-напоминания: текст без раздела о всплывших инструментах."""
        body = entry.content
        if entry.name_only_content is not None:
            body = entry.name_only_content

        if entry.from_records and entry.ephemeral:
            body = ""

        if not entry.from_records:
            body = self._pair(body, entry.ephemeral_suffix)

        if entry.from_records and body == "":
            return None

        ephemeral = False
        if not entry.from_records:
            ephemeral = entry.ephemeral or entry.ephemeral_suffix != ""

        content = body
        if not self._shape.wrap_system_text:
            content = self._reminders.wrap(body)

        return UserEntry(uuid="", content=content, is_meta=True, ephemeral=ephemeral)

    def _text_only_errors(self, entries: Sequence[Entry]) -> list[Entry]:
        """HRr: у tool_result с is_error нетекстовый контент заменяется склеенным текстом."""
        result: list[Entry] = []

        for entry in entries:
            if not isinstance(entry, UserEntry) or isinstance(entry.content, str):
                result.append(entry)
                continue
            content: list[Block] = []
            for block in entry.content:
                content.append(self._text_only_error(block))
            result.append(replace(entry, content=content))

        return result

    def _text_only_error(self, block: Block) -> Block:
        if not self._blocks.is_tool_result(block) or not block.get(Key.IS_ERROR):
            return block

        content = block.get(Key.CONTENT)
        if not isinstance(content, list):
            return block

        texts: list[str] = []
        mixed = False
        for item in content:
            if isinstance(item, dict) and self._blocks.is_text(item):
                texts.append(self._blocks.text(item))
                continue
            mixed = True

        if not mixed:
            return block

        replaced: list[Json] = []
        if texts:
            replaced.append(self._blocks.text_block("\n\n".join(texts)))

        return {**block, Key.CONTENT: replaced}

    def _push_user(self, message: UserEntry, feed: _Feed) -> None:
        last = self._last(feed.out)
        if isinstance(last, UserEntry):
            feed.out[-1] = self._merge.merge(last, message)
            return

        feed.out.append(message)

    def _wire_tool_uses(self, entry: AssistantEntry) -> AssistantEntry:
        """Вход tool_use берётся из копии с провода (wireToolInputs), если она записана."""
        changed = False
        content: list[Block] = []

        for block in entry.content:
            wired = self._wire_tool_use(block, entry)
            if wired is not block:
                changed = True
            content.append(wired)

        if not changed:
            return entry

        return replace(entry, content=content)

    def _wire_tool_use(self, block: Block, entry: AssistantEntry) -> Block:
        if not self._blocks.is_tool_use(block):
            return block

        tool_input = block.get(Key.INPUT)
        tool_id = self._blocks.id_of(block, Key.ID)
        if self._shape.echo_wire_tool_inputs and tool_id in entry.wire_tool_inputs:
            recorded = entry.wire_tool_inputs[tool_id]
            if isinstance(recorded, dict):
                tool_input = recorded

        if not self._shape.tool_search:
            return {
                Key.TYPE: BlockType.TOOL_USE,
                Key.ID: block.get(Key.ID),
                Key.NAME: block.get(Key.NAME),
                Key.INPUT: tool_input,
            }

        if tool_input is block.get(Key.INPUT):
            return block

        return {**block, Key.INPUT: tool_input}

    def _merge_assistant(self, first: AssistantEntry, second: AssistantEntry) -> AssistantEntry:
        """rCr: записи одного ответа склеиваются; пробельный текст остаётся только между мышлением."""
        if self._keeps_api_order(first) and self._keeps_api_order(second):
            return self._merge_by_api_order(first, second)

        joined = [*first.content, *second.content]
        filtered: list[Block] = []
        for position, block in enumerate(joined):
            if self._droppable_blank(joined, position):
                continue
            filtered.append(block)

        chosen = joined
        if not self._blocks.every(filtered, self._blocks.is_thinking):
            chosen = filtered

        return replace(
            first, content=self._blocks.tool_uses_last(chosen), api_block_index=None, api_block_indices=()
        )

    def _merge_by_api_order(self, first: AssistantEntry, second: AssistantEntry) -> AssistantEntry:
        """rCr при echoApiBlockOrder: блоки одного ответа встают по номеру блока в ответе API."""
        indexed = [*self._indexed(first), *self._indexed(second)]
        indexed.sort(key=self._block_order)
        content: list[Block] = []
        indices: list[int] = []
        for index, block in indexed:
            indices.append(index)
            content.append(block)

        return replace(first, content=content, api_block_index=min(indices, default=None), api_block_indices=tuple(indices))

    def _indexed(self, entry: AssistantEntry) -> Iterator[tuple[int, Block]]:
        """V4e: номер блока — из apiBlockIndices, если их столько же, сколько блоков, иначе apiBlockIndex."""
        own = entry.api_block_index
        if own is None:
            return

        for position, block in enumerate(entry.content):
            if len(entry.api_block_indices) == len(entry.content):
                yield entry.api_block_indices[position], block
                continue
            yield own, block

    def _block_order(self, item: tuple[int, Block]) -> int:
        return item[0]

    def _droppable_blank(self, blocks: Sequence[Block], position: int) -> bool:
        block = blocks[position]
        if not self._blocks.is_text(block):
            return False

        text = self._blocks.text(block)
        if text == "" or not self._js.blank(text):
            return False

        if position == 0 or position + 1 >= len(blocks):
            return True

        between = self._blocks.is_thinking(blocks[position - 1]) and self._blocks.is_thinking(blocks[position + 1])
        return not between

    def _keeps_api_order(self, entry: AssistantEntry) -> bool:
        if not self._shape.echo_api_block_order:
            return False

        return entry.api_block_index is not None

    def _surfaced(self, entry: AttachmentEntry) -> list[str]:
        """Имена для блоков tool_addition: surfacedNames, отфильтрованные списком отложенных."""
        if not self._shape.system_turns or not self._shape.tool_change_header:
            return []

        if entry.kind != AttachmentKind.DEFERRED_TOOLS_DELTA:
            return []

        if entry.uuid in self._shape.name_only_announcements:
            return []

        names = entry.attachment.get("surfacedNames")
        if not isinstance(names, list):
            return []

        surfaced: list[str] = []
        for name in names:
            if not isinstance(name, str):
                continue
            if self._shape.deferred_wire_tools is not None and name not in self._shape.deferred_wire_tools:
                continue
            surfaced.append(name)

        return surfaced

    def _promoted(self, entry: AttachmentEntry) -> bool:
        """Естественная роль system (не o_n) и записанная роль не user."""
        if entry.kind in self._USER_ROLE:
            return False

        if entry.kind == AttachmentKind.QUEUED_COMMAND and entry.rendered_role == Role.USER:
            return False

        return entry.rendered_role != Role.USER

    def _system_text(self, messages: Sequence[UserEntry]) -> str | None:
        """J4e: тексты без обёртки через перевод строки; не-текст или пустота — отказ."""
        parts: list[str] = []

        for message in messages:
            if isinstance(message.content, str):
                parts.append(self._reminders.unwrap(message.content))
                continue
            for block in message.content:
                if not self._blocks.is_text(block):
                    return None
                parts.append(self._reminders.unwrap(self._blocks.text(block)))

        joined = "\n".join(parts)
        if self._js.blank(joined):
            return None

        return joined

    def _extend_system(
        self, entry: ApiSystemEntry, text: str, ephemeral: str, name_only: str | None, additions: tuple[str, ...]
    ) -> ApiSystemEntry:
        """E8 + a6e: дописывает текст и напоминание со сроком в сообщение role:"system"."""
        base = entry.content
        carried = entry.ephemeral_suffix
        if entry.ephemeral:
            base = ""
            carried = entry.content

        plain = entry.name_only_content
        if text and (name_only is not None or plain is not None):
            head = base
            if plain is not None:
                head = plain
            tail = text
            if name_only is not None:
                tail = name_only
            plain = self._pair(head, tail)

        merged = self._pair(base, text)
        suffix = self._pair(carried, ephemeral)
        tools = (*entry.tool_additions, *additions)
        if merged == "":
            return replace(
                entry, content=suffix, ephemeral=True, ephemeral_suffix="", name_only_content=plain, tool_additions=tools
            )

        return replace(
            entry, content=merged, ephemeral=False, ephemeral_suffix=suffix, name_only_content=plain, tool_additions=tools
        )

    def _absorb_system(self, first: ApiSystemEntry, second: ApiSystemEntry) -> ApiSystemEntry:
        """Склейка соседних api_system в sCr."""
        text = second.content
        ephemeral = second.ephemeral_suffix
        if second.ephemeral:
            text = ""
            ephemeral = second.content

        merged = self._extend_system(first, text, ephemeral, second.name_only_content, second.tool_additions)
        return replace(
            merged,
            tool_removals=(*first.tool_removals, *second.tool_removals),
            from_records=first.from_records or second.from_records,
        )

    def _pair(self, first: str, second: str) -> str:
        """LC."""
        if first and second:
            return f"{first}\n\n{second}"

        if first:
            return first

        return second

    def _wrap_unwrapped(self, message: UserEntry) -> UserEntry:
        """URr."""
        if isinstance(message.content, str):
            if self._reminders.wrapped(message.content):
                return message
            return replace(message, content=self._reminders.wrap(message.content))

        content: list[Block] = []
        for block in message.content:
            if self._blocks.is_text(block) and not self._reminders.wrapped(self._blocks.text(block)):
                content.append({**block, Key.TEXT: self._reminders.wrap(self._blocks.text(block))})
                continue
            content.append(block)

        return replace(message, content=content)

    def _escape(self, message: UserEntry) -> UserEntry:
        """cyn: текст, начинающийся с поддельного <system-reminder, получает &lt;."""
        if isinstance(message.content, str):
            return replace(message, content=self._reminders.escape_forged(message.content))

        content: list[Block] = []
        for block in message.content:
            if self._blocks.is_text(block) and self._reminders.forged(self._blocks.text(block)):
                content.append({**block, Key.TEXT: self._reminders.escape_forged(self._blocks.text(block))})
                continue
            content.append(block)

        return replace(message, content=content)

    def _mark_tool_loaded(self, message: UserEntry) -> UserEntry:
        """После tool_result с tool_reference дописывается текст "Tool loaded."."""
        if isinstance(message.content, str):
            return message

        referenced = False
        for block in message.content:
            if self._blocks.is_text(block) and self._blocks.text(block).startswith(Marker.TOOL_LOADED):
                return message
            if self._blocks.is_tool_result(block) and self._blocks.has_reference(block.get(Key.CONTENT)):
                referenced = True

        if not referenced:
            return message

        return replace(message, content=[*message.content, self._blocks.text_block(Marker.TOOL_LOADED)])

    def _strip_references(self, message: UserEntry) -> UserEntry:
        """S0e: без tool search блоки tool_reference из результатов удаляются."""
        if isinstance(message.content, str):
            return message

        content: list[Block] = []
        for block in message.content:
            inner = block.get(Key.CONTENT)
            if not self._blocks.is_tool_result(block) or not isinstance(inner, list):
                content.append(block)
                continue
            if not self._blocks.has_reference(inner):
                content.append(block)
                continue
            kept: list[Json] = []
            for item in inner:
                if isinstance(item, dict) and item.get(Key.TYPE) == BlockType.TOOL_REFERENCE:
                    continue
                kept.append(item)
            if not kept:
                kept.append(self._blocks.text_block(Marker.REFERENCES_REMOVED))
            content.append({**block, Key.CONTENT: kept})

        return replace(message, content=content)

    def _moves(self, entry: Entry) -> bool:
        if isinstance(entry, AttachmentEntry):
            return True

        return self._virtual(entry)

    def _virtual(self, entry: Entry) -> bool:
        if isinstance(entry, (UserEntry, AssistantEntry)):
            return entry.is_virtual

        return False

    def _stops(self, entry: Entry) -> bool:
        if isinstance(entry, AssistantEntry):
            return True

        if not isinstance(entry, UserEntry):
            return False

        if entry.has_task_delivery:
            return True

        if isinstance(entry.content, str) or not entry.content:
            return False

        return self._blocks.is_tool_result(entry.content[0])

    def _stays_above(self, attachment: AttachmentEntry, stop: Entry) -> bool:
        """XZe && Sbe: вложение «перед закрывающей строкой» не перепрыгивает синтетический ответ."""
        if not self._shape.system_turns:
            return False

        if attachment.placement != "before_closing_line":
            return False

        if attachment.kind not in self._CLOSING_LINE:
            return False

        if not isinstance(stop, AssistantEntry):
            return False

        if stop.is_api_error or stop.is_virtual or stop.model != Synthetic.MODEL:
            return False

        if len(stop.content) != 1:
            return False

        return self._blocks.text(stop.content[0]) == Marker.NO_RESPONSE_REQUESTED

    def _has_tool_result(self, entry: UserEntry) -> bool:
        if isinstance(entry.content, str):
            return False

        return self._blocks.some(entry.content, self._blocks.is_tool_result)

    def _last(self, entries: Sequence[Entry]) -> Entry | None:
        if not entries:
            return None

        return entries[-1]


class ToolPairing:
    """Починка пар tool_use/tool_result (Jqt) и снятие блоков советника (E_)."""

    def __init__(self) -> None:
        self._blocks = BlockKit()

    def repair(self, entries: Sequence[Entry]) -> list[Entry]:
        result: list[Entry] = []
        seen: set[str] = set()
        repaired = False
        position = 0

        while position < len(entries):
            entry = entries[position]
            position += 1

            if not isinstance(entry, AssistantEntry):
                orphaned = self._without_orphans(entry, result)
                if orphaned is entry:
                    result.append(entry)
                    continue
                repaired = True
                if orphaned is not None:
                    result.append(orphaned)
                continue

            own: list[str] = []
            content = list(self._kept_blocks(entry.content, seen, own))
            changed = len(content) != len(entry.content) or self._replaced(content, entry.content)
            if not content:
                content = [self._blocks.cited_text(Marker.TOOL_USE_INTERRUPTED)]

            if changed:
                repaired = True
                result.append(replace(entry, content=content))

            if not changed:
                result.append(entry)

            following: Entry | None = None
            if position < len(entries):
                following = entries[position]

            fixed = self._fix_results(own, following)
            if fixed is None:
                continue

            repaired = True
            if isinstance(following, UserEntry):
                position += 1
            result.append(fixed)

        if not repaired:
            return list(entries)

        return result

    def strip_advisor(self, entries: Sequence[Entry]) -> list[Entry]:
        result: list[Entry] = []

        for entry in entries:
            if not isinstance(entry, AssistantEntry):
                result.append(entry)
                continue
            kept = [block for block in entry.content if not self._advisor(block)]
            if len(kept) == len(entry.content):
                result.append(entry)
                continue
            if not self._blocks.some(kept, self._substantial):
                kept.append(self._blocks.cited_text(Marker.ADVISOR_RESPONSE))
            result.append(replace(entry, content=kept))

        return result

    def _without_orphans(self, entry: Entry, result: Sequence[Entry]) -> Entry | None:
        """tool_result в user-сообщении, перед которым нет ассистента, удаляются."""
        if not isinstance(entry, UserEntry) or isinstance(entry.content, str):
            return entry

        if result and isinstance(result[-1], AssistantEntry):
            return entry

        rest = [block for block in entry.content if not self._blocks.is_tool_result(block)]
        if len(rest) == len(entry.content):
            return entry

        if rest:
            return replace(entry, content=rest)

        if result:
            return None

        return replace(entry, content=[self._blocks.text_block(Marker.ORPHANED_RESULT)])

    def _kept_blocks(self, content: Sequence[Block], seen: set[str], own: list[str]) -> Iterator[Block]:
        server_results: set[str] = set()
        for block in content:
            if Key.TOOL_USE_ID in block and not self._blocks.is_tool_result(block):
                server_results.add(self._blocks.id_of(block, Key.TOOL_USE_ID))

        for position, block in enumerate(content):
            if not self._dropped(block, seen, own, server_results):
                yield block
                continue
            if self._between_thinking(content, position):
                yield self._blocks.cited_text(Marker.TOOL_USE_REMOVED)

    def _dropped(self, block: Block, seen: set[str], own: list[str], server_results: set[str]) -> bool:
        kind = self._blocks.kind(block)
        block_id = self._blocks.id_of(block, Key.ID)

        if kind == BlockType.TOOL_USE:
            if block_id in seen:
                return True
            seen.add(block_id)
            own.append(block_id)
            return False

        if kind in (BlockType.SERVER_TOOL_USE, BlockType.MCP_TOOL_USE):
            return block_id not in server_results

        return kind == BlockType.TOOL_RESULT

    def _between_thinking(self, content: Sequence[Block], position: int) -> bool:
        if position == 0 or position + 1 >= len(content):
            return False

        return self._blocks.is_thinking(content[position - 1]) and self._blocks.is_thinking(content[position + 1])

    def _replaced(self, content: Sequence[Block], original: Sequence[Block]) -> bool:
        for kept, source in zip(content, original):
            if kept is not source:
                return True

        return False

    def _fix_results(self, own: Sequence[str], following: Entry | None) -> UserEntry | None:
        """Недостающие результаты дополняются заглушками, чужие и повторные удаляются."""
        present: list[str] = []
        duplicate = False
        blocks: list[Block] = []

        if isinstance(following, UserEntry):
            blocks = self._blocks.as_blocks(following.content)
            for block in blocks:
                if not self._blocks.is_tool_result(block):
                    continue
                result_id = self._blocks.id_of(block, Key.TOOL_USE_ID)
                if result_id in present:
                    duplicate = True
                    continue
                present.append(result_id)

        missing = [tool_id for tool_id in own if tool_id not in present]
        orphan = [result_id for result_id in present if result_id not in own]
        if not missing and not orphan and not duplicate:
            return None

        synthetic: list[Block] = []
        for tool_id in missing:
            synthetic.append(
                {
                    Key.TYPE: BlockType.TOOL_RESULT,
                    Key.TOOL_USE_ID: tool_id,
                    Key.CONTENT: Marker.RESULT_MISSING,
                    Key.IS_ERROR: True,
                }
            )

        if not isinstance(following, UserEntry):
            if not synthetic:
                return None
            return UserEntry(uuid="", content=synthetic, is_meta=True)

        kept = list(self._unique_results(blocks, set(orphan)))
        merged = [*synthetic, *kept]
        if not merged:
            return UserEntry(uuid="", content=Marker.NO_CONTENT, is_meta=True)

        return replace(following, content=merged)

    def _unique_results(self, blocks: Sequence[Block], orphan: set[str]) -> Iterator[Block]:
        taken: set[str] = set()

        for block in blocks:
            if not self._blocks.is_tool_result(block):
                yield block
                continue
            result_id = self._blocks.id_of(block, Key.TOOL_USE_ID)
            if result_id in orphan or result_id in taken:
                continue
            taken.add(result_id)
            yield block

    def _advisor(self, block: Block) -> bool:
        kind = self._blocks.kind(block)
        if kind == BlockType.ADVISOR_TOOL_RESULT:
            return True

        return kind == BlockType.SERVER_TOOL_USE and block.get(Key.NAME) == Marker.ADVISOR_TOOL

    def _substantial(self, block: Block) -> bool:
        if self._blocks.is_thinking(block):
            return False

        if not self._blocks.is_text(block):
            return True

        return self._blocks.text(block).strip() != ""


class PerTurnConfig:
    """IGt: output_config с усилием хода — на api_system после user-сообщения."""

    def apply(self, entries: Sequence[Entry], effort: str | None) -> list[Entry]:
        if effort is None:
            return list(entries)

        levels = self._levels(entries, effort)
        result: list[Entry] = []
        current: str | None = None
        pending: str | None = None

        for position, entry in enumerate(entries):
            if pending is not None and not isinstance(entry, UserEntry):
                current = pending
                config: dict[str, Json] = {Key.EFFORT: pending}
                pending = None
                if isinstance(entry, ApiSystemEntry):
                    result.append(replace(entry, output_config=config))
                    continue
                result.append(ApiSystemEntry(content="", output_config=config))

            result.append(entry)

            if isinstance(entry, UserEntry):
                pending = None
                if levels[position] != current:
                    pending = levels[position]

        if pending is not None:
            result.append(ApiSystemEntry(content="", output_config={Key.EFFORT: pending}))

        return result

    def _levels(self, entries: Sequence[Entry], effort: str) -> list[str]:
        """fWo: усилие позиции — усилие ближайшего ответа ассистента на ней или после неё."""
        levels: list[str] = []
        level = effort

        for entry in reversed(entries):
            if isinstance(entry, AssistantEntry) and not entry.is_api_error:
                level = self._own_level(entry, level)
            levels.append(level)

        levels.reverse()
        return levels

    def _own_level(self, entry: AssistantEntry, fallback: str) -> str:
        if entry.per_turn_effort is not None:
            return entry.per_turn_effort

        if entry.effort is not None:
            return entry.effort

        return fallback


class WireSerializer:
    """vzo: внутренние сообщения → сообщения провода и путь метки кэша."""

    def __init__(self, shape: WireShape) -> None:
        self._shape = shape
        self._blocks = BlockKit()
        self._marks = CacheMarks(shape)
        self._js = JsText()

    def serialize(
        self, entries: Sequence[Entry], skip_cache_write: bool, fork_pin: bool
    ) -> tuple[list[dict[str, Json]], list[MarkPath]]:
        marked: list[int] = []
        if self._shape.prompt_caching:
            marked = self._marks.choose(entries, skip_cache_write, fork_pin)

        messages: list[dict[str, Json]] = []
        paths: list[MarkPath] = []

        for position, entry in enumerate(entries):
            base, cleared = self._scoped(entry, position == len(entries) - 1)
            if base is not None:
                message, block = self._message(base, position in marked)
                if block >= 0:
                    paths.append((Key.MESSAGES, len(messages), Key.CONTENT, block))
                messages.append(message)
            if cleared != "":
                messages.append(
                    {Key.ROLE: Role.SYSTEM, Key.CONTENT: cleared, Key.CLEAR_AT: Marker.NEXT_USER_MESSAGE}
                )

        return messages, paths

    def _scoped(self, entry: Entry, last: bool) -> tuple[Entry | None, str]:
        """Yzt/Xzt: напоминание со сроком уходит отдельным сообщением с clear_at после основы.

        Возвращает основу сообщения (None — основа пуста и не отправляется) и
        текст сообщения со сроком (пустая строка — такого сообщения нет).
        """
        if not isinstance(entry, ApiSystemEntry):
            return entry, ""

        scoped = entry.ephemeral_suffix
        base = entry.content
        if entry.ephemeral:
            scoped = entry.content
            base = ""

        if not entry.ephemeral and scoped == "":
            return entry, ""

        stale = entry.from_records and not last
        if not self._shape.kept_reminders and not stale:
            return entry, ""

        cleared = ""
        if self._shape.kept_reminders and not self._js.blank(scoped):
            cleared = scoped

        stripped = replace(entry, content=base, ephemeral=False, ephemeral_suffix="")
        if base != "" or entry.tool_additions or entry.tool_removals or entry.output_config is not None:
            return stripped, cleared

        return None, cleared

    def _message(self, entry: Entry, marked: bool) -> tuple[dict[str, Json], int]:
        if isinstance(entry, UserEntry):
            return self._user(entry, marked)

        if isinstance(entry, ApiSystemEntry):
            return self._system(entry, marked)

        if isinstance(entry, AssistantEntry):
            return self._assistant(entry, marked)

        raise AssembleError(f"serializing messages: unexpected entry {type(entry).__name__} after normalization")

    def _user(self, entry: UserEntry, marked: bool) -> tuple[dict[str, Json], int]:
        """$Go: пробельные текстовые блоки отбрасываются (Eqt), метка — на последнем блоке."""
        if isinstance(entry.content, str):
            if not marked:
                return {Key.ROLE: Role.USER, Key.CONTENT: entry.content}, -1
            return {Key.ROLE: Role.USER, Key.CONTENT: [self._blocks.text_block(entry.content)]}, 0

        content: list[Json] = []
        for block in entry.content:
            if self._blocks.is_text(block) and self._blank_text(block):
                continue
            content.append(block)

        if not content:
            content.append(self._blocks.text_block(Marker.NO_CONTENT))

        mark = -1
        if marked:
            mark = len(content) - 1

        return {Key.ROLE: Role.USER, Key.CONTENT: content}, mark

    def _system(self, entry: ApiSystemEntry, marked: bool) -> tuple[dict[str, Json], int]:
        """Ветка api_system в vzo и tBo: text, tool_removal, tool_addition, затем суффикс."""
        message: dict[str, Json] = {Key.ROLE: Role.SYSTEM}
        mark = -1

        if entry.tool_additions or entry.tool_removals:
            content: list[Json] = []
            if entry.content:
                content.append(self._blocks.text_block(entry.content))
            for name in entry.tool_removals:
                content.append(self._tool_change(BlockType.TOOL_REMOVAL, name))
            for name in entry.tool_additions:
                content.append(self._tool_change(BlockType.TOOL_ADDITION, name))
            if marked:
                mark = len(content) - 1
            if entry.ephemeral_suffix:
                content.append(self._blocks.text_block(entry.ephemeral_suffix))
            message[Key.CONTENT] = content
        elif marked:
            blocks: list[Json] = [self._blocks.text_block(entry.content)]
            if entry.ephemeral_suffix:
                blocks.append(self._blocks.text_block(entry.ephemeral_suffix))
            message[Key.CONTENT] = blocks
            mark = 0
        else:
            message[Key.CONTENT] = self._plain_system(entry)

        if entry.output_config is not None:
            message[Key.OUTPUT_CONFIG] = dict(entry.output_config)

        return message, mark

    def _plain_system(self, entry: ApiSystemEntry) -> Json:
        if entry.content and entry.ephemeral_suffix:
            return f"{entry.content}\n\n{entry.ephemeral_suffix}"

        if entry.content:
            return entry.content

        if entry.ephemeral_suffix:
            return entry.ephemeral_suffix

        return []

    def _assistant(self, entry: AssistantEntry, marked: bool) -> tuple[dict[str, Json], int]:
        """BGo: метка — на последнем блоке, если он её допускает."""
        content: list[Json] = list(entry.content)
        mark = -1

        if marked and entry.content and self._marks.stampable(entry.content[-1]):
            mark = len(content) - 1

        return {Key.ROLE: Role.ASSISTANT, Key.CONTENT: content}, mark

    def _tool_change(self, kind: BlockType, name: str) -> Block:
        return {Key.TYPE: kind, Key.TOOL: {Key.TYPE: BlockType.TOOL_REFERENCE, Key.NAME: name}}

    def _blank_text(self, block: Block) -> bool:
        text = block.get(Key.TEXT)
        if not isinstance(text, str):
            return True

        return self._js.blank(text)


class EntryFactory:
    """Строка журнала → запись внутреннего списка клиента.

    Создаётся Assembler'ом; разбирает user, assistant, attachment и system в
    типизированные записи, с которыми работает Normalizer.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._blocks = BlockKit()

    def entry(self, row: Row) -> Entry:
        return self._entry(row.line, row.raw)

    def _entry(self, number: int, raw: Mapping[str, Json]) -> Entry:
        kind = raw.get(Key.TYPE)
        uuid = self._uuid(number, raw)

        if kind == RecordType.SYSTEM:
            content = raw.get(Key.CONTENT)
            if not isinstance(content, str):
                content = ""
            preserved, anchor = self._preserved(raw)
            return SystemEntry(
                uuid=uuid, subtype=self._text(raw, "subtype"), content=content, preserved=preserved, anchor=anchor
            )

        if kind == RecordType.ATTACHMENT:
            return self._attachment(number, uuid, raw)

        message = raw.get("message")
        if not isinstance(message, dict):
            raise AssembleError(
                f"{self._path}:{number}: record {kind} expects message as an object, got {type(message).__name__}"
            )

        if kind == RecordType.USER:
            origin = raw.get("origin")
            origin_kind = ""
            if isinstance(origin, dict):
                origin_kind = self._text(origin, "kind")
            return UserEntry(
                uuid=uuid,
                content=self._content(number, message),
                is_meta=raw.get("isMeta") is True,
                is_virtual=raw.get("isVirtual") is True,
                has_task_delivery=raw.get("taskDelivery") is not None,
                origin_kind=origin_kind,
                interrupted_by_shutdown=raw.get("interruptedByShutdown") is True,
            )

        return self._assistant(number, uuid, raw, message)

    def _assistant(
        self, number: int, uuid: str, raw: Mapping[str, Json], message: Mapping[str, Json]
    ) -> AssistantEntry:
        content = self._content(number, message)
        if isinstance(content, str):
            text = content
            if text == "":
                text = Marker.NO_CONTENT
            content = [self._blocks.text_block(text)]

        message_id = message.get(Key.ID)
        if not isinstance(message_id, str):
            message_id = None

        wire = raw.get("wireToolInputs")
        if not isinstance(wire, dict):
            wire = {}

        index = raw.get("apiBlockIndex")
        if not isinstance(index, int):
            index = None

        return AssistantEntry(
            uuid=uuid,
            message_id=message_id,
            model=self._text(message, "model"),
            content=content,
            is_virtual=raw.get("isVirtual") is True,
            is_api_error=raw.get("isApiErrorMessage") is True,
            effort=self._optional(raw, "effort"),
            per_turn_effort=self._optional(raw, "perTurnEffort"),
            api_block_index=index,
            wire_tool_inputs=wire,
        )

    def _preserved(self, raw: Mapping[str, Json]) -> tuple[tuple[str, ...], str]:
        """compactMetadata.preservedMessages: uuid сохранённого хвоста и якорь, за которым он стоит."""
        metadata = raw.get("compactMetadata")
        if not isinstance(metadata, dict):
            return (), ""

        messages = metadata.get("preservedMessages")
        if not isinstance(messages, dict):
            return (), ""

        uuids = messages.get("allUuids")
        if not isinstance(uuids, list):
            uuids = messages.get("uuids")
        if not isinstance(uuids, list):
            return (), ""

        names: list[str] = []
        for uuid in uuids:
            if isinstance(uuid, str):
                names.append(uuid)

        return tuple(names), self._text(messages, "anchorUuid")

    def _attachment(self, number: int, uuid: str, raw: Mapping[str, Json]) -> AttachmentEntry:
        attachment = raw.get("attachment")
        if not isinstance(attachment, dict):
            raise AssembleError(
                f"{self._path}:{number}: record attachment expects attachment as an object, "
                f"got {type(attachment).__name__}"
            )

        rendered: list[Content] = []
        recorded = raw.get("rendered")
        has_rendered = isinstance(recorded, list)
        if isinstance(recorded, list):
            for item in recorded:
                if not isinstance(item, dict):
                    raise AssembleError(f"{self._path}:{number}: rendered expects objects with content, got {item!r}")
                rendered.append(self._content(number, item))

        return AttachmentEntry(
            uuid=uuid,
            kind=self._text(attachment, Key.TYPE),
            attachment=attachment,
            rendered=rendered,
            has_rendered=has_rendered,
            rendered_role=self._text(raw, "renderedRole"),
            placement=self._text(raw, "placement"),
        )

    def _content(self, number: int, holder: Mapping[str, Json]) -> Content:
        content = holder.get(Key.CONTENT)
        if isinstance(content, str):
            return content

        if not isinstance(content, list):
            raise AssembleError(
                f"{self._path}:{number}: content expects a string or a list of blocks, got {type(content).__name__}"
            )

        blocks: list[Block] = []
        for block in content:
            if not isinstance(block, dict):
                raise AssembleError(f"{self._path}:{number}: content block expects an object, got {block!r}")
            blocks.append(block)

        return blocks

    def _uuid(self, number: int, raw: Mapping[str, Json]) -> str:
        uuid = raw.get("uuid")
        if not isinstance(uuid, str):
            raise AssembleError(f"{self._path}:{number}: history record expects uuid as a string, got {uuid!r}")

        return uuid

    def _text(self, holder: Mapping[str, Json], key: str) -> str:
        value = holder.get(key)
        if isinstance(value, str):
            return value

        return ""

    def _optional(self, holder: Mapping[str, Json], key: str) -> str | None:
        value = holder.get(key)
        if isinstance(value, str):
            return value

        return None


class SessionShape:
    """Режимы сборки и усилие запроса по записям журнала.

    Режимы берутся из последних prompt_snapshot и признаков в записях
    истории; создаётся Assembler'ом и SummaryRequestBuilder'ом.
    """

    def __init__(self, journal: Journal) -> None:
        self._journal = journal

    def shape(self) -> WireShape:
        """Режимы из последних prompt_snapshot журнала и признаков в записях истории."""
        snapshot: dict[str, Json] = {}
        deferred: set[str] = set()
        has_record = False
        announced = False
        listed: bool | None = None
        advisor = False
        per_turn = False

        for _, raw in self._journal.entries():
            attachment = raw.get("attachment")
            if raw.get(Key.TYPE) == RecordType.ASSISTANT:
                advisor = advisor or isinstance(raw.get("advisorModel"), str)
                per_turn = per_turn or isinstance(raw.get("perTurnEffort"), str)
            if not isinstance(attachment, dict):
                continue
            kind = attachment.get(Key.TYPE)
            if kind == AttachmentKind.PROMPT_SNAPSHOT:
                for key in ("systemTurns", "toolChangeHeader", "reminderFold", "echoWireToolInputs", "keptReminders"):
                    if isinstance(attachment.get(key), bool):
                        snapshot[key] = attachment[key]
                tools = attachment.get("tools")
                if isinstance(tools, list):
                    listed = Marker.TOOL_SEARCH in self._entry_names(tools)
            if kind == AttachmentKind.DEFERRED_TOOLS_DELTA and attachment.get("addedNames"):
                announced = True
            if kind == AttachmentKind.DEFERRED_TOOLS_RECORD:
                has_record = True
                deferred.update(self._entry_names(attachment.get("entries")))

        wire_tools: frozenset[str] | None = None
        if has_record:
            wire_tools = frozenset(deferred)

        tool_search = announced
        if listed is not None:
            tool_search = listed

        return WireShape(
            system_turns=snapshot.get("systemTurns") is True,
            tool_change_header=snapshot.get("toolChangeHeader") is True,
            reminder_fold=snapshot.get("reminderFold") is True,
            echo_wire_tool_inputs=snapshot.get("echoWireToolInputs") is True,
            tool_search=tool_search,
            advisor=advisor,
            per_turn_effort=per_turn,
            echo_api_block_order=False,
            wrap_system_text=False,
            prompt_caching=True,
            kept_reminders=snapshot.get("keptReminders") is True,
            deferred_wire_tools=wire_tools,
            name_only_announcements=frozenset(),
        )

    def name_only_before(self, line: int) -> frozenset[str]:
        """uuid дельт, объявленных только именем, по записям deferred_tools_record прежних запросов.

        Запись запроса пишется вместе с ответом, прямо перед его первой
        строкой; известной к строке line она становится, когда за ней уже
        есть строка user или assistant.
        """
        known: set[str] = set()
        waiting: set[str] = set()

        for number, raw in self._journal.entries():
            if number >= line:
                break
            kind = raw.get(Key.TYPE)
            if kind in (RecordType.USER, RecordType.ASSISTANT):
                known.update(waiting)
                waiting.clear()
                continue
            attachment = raw.get("attachment")
            if not isinstance(attachment, dict) or attachment.get(Key.TYPE) != AttachmentKind.DEFERRED_TOOLS_RECORD:
                continue
            waiting.update(self._strings(attachment.get("nameOnlyAnnouncements")))

        return frozenset(known)

    def effort_at(self, line: int) -> str | None:
        """Усилие запроса: его несёт первый ответ ассистента, записанный на строке момента или после неё."""
        before: str | None = None

        for number, raw in self._journal.entries():
            if raw.get(Key.TYPE) != RecordType.ASSISTANT or raw.get("isApiErrorMessage") is True:
                continue
            effort = raw.get("perTurnEffort")
            if not isinstance(effort, str):
                continue
            if number >= line:
                return effort
            before = effort

        return before

    def _entry_names(self, entries: Json) -> Iterator[str]:
        if not isinstance(entries, list):
            return

        for item in entries:
            if isinstance(item, dict) and isinstance(item.get(Key.NAME), str):
                yield str(item[Key.NAME])

    def _strings(self, items: Json) -> Iterator[str]:
        if not isinstance(items, list):
            return

        for item in items:
            if isinstance(item, str):
                yield item


class Assembler:
    """Конвейер сборки messages запроса (Iqt + vzo) над списком записей в памяти процесса.

    Историю перед строкой запроса даёт SessionMemory (загрузчик плюс строки
    процесса); дальше работают Normalizer, ToolPairing, PerTurnConfig и
    WireSerializer. Починку при чтении журнала делает загрузчик, здесь —
    только починка при сборке (Jqt). Им пользуются RequestBuilder,
    SummaryRequestBuilder и сверка.
    """

    def __init__(self, memory: SessionMemory, echo_api_block_order: bool = False) -> None:
        self._memory = memory
        self._factory = EntryFactory(memory.journal.path)
        self._session = SessionShape(memory.journal)
        self.shape = replace(self._session.shape(), echo_api_block_order=echo_api_block_order)
        self._normalizers: dict[frozenset[str], Normalizer] = {}
        self._pairing = ToolPairing()
        self._per_turn = PerTurnConfig()
        self._serializer = WireSerializer(self.shape)

    def moments(self) -> list[Moment]:
        result: list[Moment] = []
        for moment in self._memory.moments():
            result.append(self.moment_at(moment.line, moment.ordinal))

        return result

    def moment_at(self, line: int, ordinal: int) -> Moment:
        return Moment(ordinal=ordinal, line=line, effort=self._session.effort_at(line))

    def history_before(self, line: int) -> list[Entry]:
        """Записи списка клиента перед строкой line, от последней границы сжатия."""
        try:
            window = self._memory.window_before(line)
        except LoaderError as exc:
            raise AssembleError(f"restoring history of {self._memory.journal.path} before line {line} failed: {exc}") from exc

        entries: list[Entry] = []
        for row in window.rows:
            entries.append(self._factory.entry(row))

        return entries

    def assemble(self, moment: Moment, fork_pin: bool) -> Assembled:
        """Сообщения запроса со строки moment.line; fork_pin — вторая метка кэша запроса без нити."""
        notes: list[str] = []
        entries = self.entries(self.history_before(moment.line), moment, notes)
        messages, marks = self._serializer.serialize(entries, False, fork_pin)
        return Assembled(ordinal=moment.ordinal, line=moment.line, messages=messages, marks=marks, notes=notes)

    def entries(self, history: Sequence[Entry], moment: Moment, notes: list[str]) -> list[Entry]:
        """Нормализованный список сообщений перед сериализацией."""
        effort = moment.effort
        entries = self._normalizer_at(moment.line).run(history, notes)
        entries = self._pairing.repair(entries)

        if not self.shape.advisor:
            entries = self._pairing.strip_advisor(entries)

        if not self.shape.per_turn_effort:
            effort = None

        return self._per_turn.apply(entries, effort)

    def serialize(self, entries: Sequence[Entry], skip_cache_write: bool) -> tuple[list[dict[str, Json]], list[MarkPath]]:
        return self._serializer.serialize(entries, skip_cache_write, False)

    def _normalizer_at(self, line: int) -> Normalizer:
        """Нормализатор с объявлениями «только именем», известными клиенту до запроса со строки line."""
        names = self._session.name_only_before(line)
        known = self._normalizers.get(names)
        if known is not None:
            return known

        created = Normalizer(replace(self.shape, name_only_announcements=names))
        self._normalizers[names] = created
        return created
