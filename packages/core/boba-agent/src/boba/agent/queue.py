"""Очередь ввода: единственный путь, которым что-либо попадает в разговор.

Устройство — docs/prompt_assembly/specs/14_queue_kinds.md и план, раздел 5.7:
элементы с видом, приоритетом и источником, выбор следующего по приоритету
и порядку постановки, склейка совместимых сообщений, протокол действий
записями `queue-operation` в сессии. Сама очередь в журнале не хранится и по
нему не восстанавливается.

Ошибки:
QueueRefusedError — элемент не принят: вид не поддержан в этой версии или превышен
    предел; следа в протоколе такой элемент не оставляет.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from boba.agent.ids import Clock, IdMint
from boba.agent.records import (
    QueueOperation,
    QueueOperationRecord,
    QueueRemoveReason,
)
from boba.agent.session import Session

__all__ = [
    "InputQueue",
    "QueueEntry",
    "QueueItem",
    "QueueKind",
    "QueuePriority",
    "QueueRefusedError",
    "QueueSource",
    "Taken",
]


class QueueRefusedError(Exception):
    """Элемент отвергнут на входе очереди."""


class QueueKind(StrEnum):
    """Виды элементов; poll-event и orphaned-permission — заготовки без поведения."""

    PROMPT = "prompt"
    TASK_NOTIFICATION = "task-notification"
    POLL_EVENT = "poll-event"
    ORPHANED_PERMISSION = "orphaned-permission"

    @property
    def supported(self) -> bool:
        return self in (QueueKind.PROMPT, QueueKind.TASK_NOTIFICATION)

    @property
    def default_priority(self) -> QueuePriority:
        if self is QueueKind.PROMPT:
            return QueuePriority.NEXT

        return QueuePriority.LATER


class QueuePriority(StrEnum):
    """Приоритеты элементов; меньший ранг — раньше."""

    NOW = "now"
    NEXT = "next"
    LATER = "later"

    @property
    def rank(self) -> int:
        ranks = {
            QueuePriority.NOW: 0,
            QueuePriority.NEXT: 1,
            QueuePriority.LATER: 2,
        }

        return ranks[self]


class QueueSource(StrEnum):
    """Кто создал элемент."""

    HUMAN = "human"
    TASK = "task"
    AGENT = "agent"


class QueueEntry(BaseModel):
    """Что ставят в очередь: вид, текст, приоритет и источник."""

    model_config = ConfigDict(frozen=True)

    kind: QueueKind
    text: str
    priority: QueuePriority | None = None
    source: QueueSource = QueueSource.HUMAN
    is_meta: bool = False
    unrecorded: bool = False


class QueueItem(BaseModel):
    """Элемент очереди."""

    model_config = ConfigDict(frozen=True)

    id: str
    kind: QueueKind
    text: str
    priority: QueuePriority
    enqueued_at: str
    source: QueueSource
    is_meta: bool = False
    unrecorded: bool = False

    def compatible_with(self, other: QueueItem) -> bool:
        """Совместимые сообщения уходят одним ходом: тот же вид prompt,
        источник, приоритет и служебный признак."""
        if self.kind is not QueueKind.PROMPT or other.kind is not QueueKind.PROMPT:
            return False

        if self.source is not other.source:
            return False

        if self.priority is not other.priority:
            return False

        return self.is_meta == other.is_meta


@dataclass(frozen=True)
class Taken:
    """Что взято из очереди на ход: элементы и их склеенный текст."""

    items: Sequence[QueueItem]

    @property
    def head(self) -> QueueItem:
        return self.items[0]

    @property
    def text(self) -> str:
        parts: list[str] = []
        for item in self.items:
            parts.append(item.text)

        return "\n".join(parts)


class InputQueue:
    """Очередь ввода одной сессии с протоколом действий в её записях."""

    NOTIFICATION_MAX_CHARS: ClassVar[int] = 100_000
    CUT_MARK: ClassVar[str] = "\n\n[... middle of the notification cut ...]\n\n"

    def __init__(self, session: Session, clock: Clock, ids: IdMint) -> None:
        self._session = session
        self._clock = clock
        self._ids = ids
        self._items: list[QueueItem] = []
        self._changed = asyncio.Event()
        self._now_arrived = asyncio.Event()

    @property
    def now_arrived(self) -> asyncio.Event:
        """Взводится, когда появился элемент `now`: подписчик прерывает ход."""
        return self._now_arrived

    def __len__(self) -> int:
        return len(self._items)

    def items(self) -> Sequence[QueueItem]:
        return list(self._items)

    def enqueue(self, entry: QueueEntry) -> QueueItem:
        kind = entry.kind
        if not kind.supported:
            msg = (
                f"Queue items of kind '{kind.value}' are not supported yet. Only "
                "'prompt' and 'task-notification' can be queued in this version."
            )
            raise QueueRefusedError(msg)

        priority = entry.priority
        if priority is None:
            priority = kind.default_priority

        item = QueueItem(
            id=self._ids.uuid(),
            kind=kind,
            text=self._bounded(kind, entry.text),
            priority=priority,
            enqueued_at=self._clock.now(),
            source=entry.source,
            is_meta=entry.is_meta,
            unrecorded=entry.unrecorded,
        )
        self._items.append(item)
        if not item.unrecorded:
            self._record(QueueOperation.ENQUEUE, content=item.text)

        self._changed.set()
        if priority is QueuePriority.NOW:
            self._now_arrived.set()

        return item

    def take(self) -> Taken | None:
        """Голова очереди и совместимые с ней сообщения; None — очередь пуста."""
        head = self._head()
        if head is None:
            return None

        taken = [head]
        position = self._items.index(head) + 1
        while position < len(self._items):
            candidate = self._items[position]
            if not head.compatible_with(candidate):
                break

            taken.append(candidate)
            position += 1

        for item in taken:
            self._items.remove(item)
            if not item.unrecorded:
                self._record(QueueOperation.DEQUEUE, command_uuid=item.id)

        self._refresh_now()

        return Taken(items=taken)

    def absorb(self) -> Sequence[QueueItem]:
        """Элементы `now` и `next` для подмешивания посреди хода."""
        absorbed: list[QueueItem] = []
        for item in list(self._items):
            if item.priority is QueuePriority.LATER:
                continue

            self._items.remove(item)
            absorbed.append(item)
            if not item.unrecorded:
                self._record(
                    QueueOperation.REMOVE,
                    reason=QueueRemoveReason.ABSORBED_MID_TURN,
                    command_uuid=item.id,
                )

        self._refresh_now()

        return absorbed

    def remove(self, item_id: str, reason: QueueRemoveReason) -> bool:
        item = self._find(item_id)
        if item is None:
            return False

        self._items.remove(item)
        if not item.unrecorded:
            self._record(QueueOperation.REMOVE, reason=reason, command_uuid=item.id)

        self._refresh_now()

        return True

    def clear(self, reason: QueueRemoveReason) -> int:
        """Убирает все элементы с одной причиной; возвращает их число."""
        count = 0
        for item in list(self._items):
            self.remove(item.id, reason)
            count += 1

        return count

    def pop_all(self) -> Sequence[QueueItem]:
        """Забирает все элементы без обработки одной записью `popAll`."""
        popped = list(self._items)
        self._items.clear()
        if popped:
            self._record(QueueOperation.POP_ALL)

        self._refresh_now()

        return popped

    def pop_one(self, item_id: str) -> QueueItem | None:
        item = self._find(item_id)
        if item is None:
            return None

        self._items.remove(item)
        self._record(QueueOperation.POP_ONE, command_uuid=item.id)
        self._refresh_now()

        return item

    def raise_to_now(self, item_id: str) -> bool:
        return self._reprioritise(item_id, QueuePriority.NOW)

    def lower(self, item_id: str, priority: QueuePriority) -> bool:
        return self._reprioritise(item_id, priority)

    def has_now(self) -> bool:
        return any(self._now_flags())

    def _now_flags(self) -> Iterator[bool]:
        for item in self._items:
            yield item.priority is QueuePriority.NOW

    async def wait_changed(self) -> None:
        """Ждёт следующего изменения очереди."""
        await self._changed.wait()
        self._changed.clear()

    def wake(self) -> None:
        """Будит ожидающего без изменения очереди: остановка процесса."""
        self._changed.set()

    def _reprioritise(self, item_id: str, priority: QueuePriority) -> bool:
        item = self._find(item_id)
        if item is None:
            return False

        position = self._items.index(item)
        self._items[position] = item.model_copy(update={"priority": priority})
        if priority is QueuePriority.NOW:
            self._now_arrived.set()

        self._refresh_now()
        self._changed.set()

        return True

    def _head(self) -> QueueItem | None:
        best: QueueItem | None = None
        for item in self._items:
            if best is None or item.priority.rank < best.priority.rank:
                best = item

        return best

    def _find(self, item_id: str) -> QueueItem | None:
        for item in self._items:
            if item.id == item_id:
                return item

        return None

    def _refresh_now(self) -> None:
        if not self.has_now():
            self._now_arrived.clear()

    def _bounded(self, kind: QueueKind, text: str) -> str:
        """Сообщения людей без предела; у длинного уведомления вырезается середина."""
        if kind is not QueueKind.TASK_NOTIFICATION:
            return text

        if len(text) <= self.NOTIFICATION_MAX_CHARS:
            return text

        keep = (self.NOTIFICATION_MAX_CHARS - len(self.CUT_MARK)) // 2

        return text[:keep] + self.CUT_MARK + text[-keep:]

    def _record(
        self,
        operation: QueueOperation,
        content: str | None = None,
        reason: QueueRemoveReason | None = None,
        command_uuid: str | None = None,
    ) -> None:
        self._session.add(
            QueueOperationRecord(
                timestamp=self._clock.now(),
                session_id=self._session.id.value,
                operation=operation,
                content=content,
                reason=reason,
                command_uuid=command_uuid,
            )
        )
