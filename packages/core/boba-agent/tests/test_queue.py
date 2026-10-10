"""Очередь ввода: порядок выбора, склейка, протокол в журнале, пределы."""

from __future__ import annotations

import pytest

from boba.agent.history import SessionId
from boba.agent.ids import IdMint, SystemClock
from boba.agent.queue import (
    InputQueue,
    QueueEntry,
    QueueKind,
    QueuePriority,
    QueueRefusedError,
    QueueSource,
)
from boba.agent.records import QueueOperation, QueueOperationRecord, QueueRemoveReason
from boba.agent.session import Session

SESSION = SessionId(value="q-1")


def fresh() -> tuple[Session, InputQueue]:
    session = Session(SESSION)

    return session, InputQueue(session, SystemClock(), IdMint())


def protocol(session: Session) -> list[tuple[str, str | None]]:
    rows: list[tuple[str, str | None]] = []
    for record in session.service():
        if not isinstance(record, QueueOperationRecord):
            continue

        reason: str | None = None
        if record.reason is not None:
            reason = record.reason.value

        rows.append((record.operation.value, reason))

    return rows


def prompt(text: str, priority: QueuePriority | None = None) -> QueueEntry:
    return QueueEntry(kind=QueueKind.PROMPT, text=text, priority=priority)


class TestOrder:
    def test_lower_rank_goes_first_then_enqueue_order(self) -> None:
        _, queue = fresh()
        queue.enqueue(prompt("later one", QueuePriority.LATER))
        queue.enqueue(
            QueueEntry(
                kind=QueueKind.TASK_NOTIFICATION,
                text="task done",
                source=QueueSource.TASK,
            )
        )
        queue.enqueue(prompt("next one"))
        queue.enqueue(prompt("now one", QueuePriority.NOW))
        queue.enqueue(prompt("next two"))

        order: list[str] = []
        while True:
            taken = queue.take()
            if taken is None:
                break

            order.append(taken.text)

        assert order == ["now one", "next one\nnext two", "later one", "task done"]

    def test_compatible_prompts_are_joined_only_when_adjacent(self) -> None:
        _, queue = fresh()
        queue.enqueue(prompt("a"))
        queue.enqueue(prompt("b"))
        queue.enqueue(prompt("c", QueuePriority.LATER))
        queue.enqueue(prompt("d"))

        first = queue.take()

        assert first is not None
        assert first.text == "a\nb"
        assert [item.text for item in first.items] == ["a", "b"]
        assert len(queue) == 2

    def test_notification_is_not_joined_with_prompts(self) -> None:
        _, queue = fresh()
        queue.enqueue(prompt("a", QueuePriority.LATER))
        queue.enqueue(
            QueueEntry(
                kind=QueueKind.TASK_NOTIFICATION,
                text="done",
                priority=QueuePriority.LATER,
                source=QueueSource.TASK,
            )
        )

        first = queue.take()

        assert first is not None
        assert first.text == "a"
        assert len(queue) == 1

    def test_empty_queue_takes_nothing(self) -> None:
        _, queue = fresh()

        assert queue.take() is None


class TestProtocol:
    def test_enqueue_dequeue_and_remove_are_recorded(self) -> None:
        session, queue = fresh()
        kept = queue.enqueue(prompt("keep"))
        dropped = queue.enqueue(prompt("drop", QueuePriority.LATER))

        queue.remove(dropped.id, QueueRemoveReason.CLEARED_ON_CANCEL)
        taken = queue.take()

        assert taken is not None
        assert taken.head.id == kept.id
        assert protocol(session) == [
            ("enqueue", None),
            ("enqueue", None),
            ("remove", "cleared_on_cancel"),
            ("dequeue", None),
        ]
        records = [r for r in session.service() if isinstance(r, QueueOperationRecord)]
        assert records[0].content == "keep"
        assert records[2].command_uuid == dropped.id
        assert records[3].command_uuid == kept.id

    def test_absorb_takes_now_and_next_only(self) -> None:
        session, queue = fresh()
        queue.enqueue(prompt("next"))
        queue.enqueue(prompt("later", QueuePriority.LATER))
        queue.enqueue(prompt("now", QueuePriority.NOW))

        absorbed = queue.absorb()

        assert sorted(item.text for item in absorbed) == ["next", "now"]
        assert [item.text for item in queue.items()] == ["later"]
        assert protocol(session)[-2:] == [
            ("remove", "absorbed_mid_turn"),
            ("remove", "absorbed_mid_turn"),
        ]

    def test_unrecorded_item_leaves_no_trace(self) -> None:
        session, queue = fresh()
        queue.enqueue(prompt("quiet").model_copy(update={"unrecorded": True}))

        taken = queue.take()

        assert taken is not None
        assert protocol(session) == []

    def test_pop_all_is_one_record(self) -> None:
        session, queue = fresh()
        queue.enqueue(prompt("a"))
        queue.enqueue(prompt("b"))

        popped = queue.pop_all()

        assert len(popped) == 2
        assert protocol(session)[-1] == (QueueOperation.POP_ALL.value, None)
        assert len(queue) == 0


class TestPriorityChanges:
    def test_raise_to_now_sets_the_signal_and_lower_clears_it(self) -> None:
        _, queue = fresh()
        item = queue.enqueue(prompt("a", QueuePriority.LATER))

        assert not queue.now_arrived.is_set()
        assert queue.raise_to_now(item.id)
        assert queue.has_now()
        assert queue.now_arrived.is_set()
        assert queue.lower(item.id, QueuePriority.LATER)
        assert not queue.now_arrived.is_set()

    def test_unknown_item_is_not_found(self) -> None:
        _, queue = fresh()

        assert not queue.raise_to_now("missing")
        assert not queue.remove("missing", QueueRemoveReason.DROPPED_BY_HOOK)


class TestLimits:
    def test_unsupported_kind_is_refused_without_a_trace(self) -> None:
        session, queue = fresh()

        with pytest.raises(QueueRefusedError, match="poll-event"):
            queue.enqueue(QueueEntry(kind=QueueKind.POLL_EVENT, text="x"))

        assert protocol(session) == []
        assert len(queue) == 0

    def test_long_notification_loses_its_middle(self) -> None:
        _, queue = fresh()
        text = "a" * 60_000 + "MIDDLE" + "b" * 60_000

        item = queue.enqueue(
            QueueEntry(
                kind=QueueKind.TASK_NOTIFICATION, text=text, source=QueueSource.TASK
            )
        )

        assert len(item.text) <= InputQueue.NOTIFICATION_MAX_CHARS
        assert "MIDDLE" not in item.text
        assert item.text.startswith("aaaa")
        assert item.text.endswith("bbbb")
        assert InputQueue.CUT_MARK in item.text

    def test_prompts_have_no_limit(self) -> None:
        _, queue = fresh()
        text = "a" * 300_000

        item = queue.enqueue(prompt(text))

        assert len(item.text) == 300_000


class TestWaiting:
    async def test_wait_changed_wakes_on_enqueue(self) -> None:
        _, queue = fresh()
        queue.enqueue(prompt("a"))

        await queue.wait_changed()

        assert queue.take() is not None


pytestmark = pytest.mark.anyio
