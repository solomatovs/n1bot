"""Сессия в памяти: упорядоченный список записей и его проекции.

Главная копия истории — этот список; журнал на диске или в базе — его
отражение. Записи добавляет цикл хода и обработчики управляющих сообщений
в одном цикле событий; сессия выдаёт seq и держит проекции: разговор от
последней полной границы сжатия с перенесённым хвостом и служебный хвост.

Ошибки:
SessionError — запись уже несёт seq, или восстановленные записи идут не по
    порядку seq.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from enum import StrEnum
from typing import TypeVar

from boba.agent.history import SessionId
from boba.agent.records import (
    AssistantRecord,
    ChainRecord,
    CompactBoundaryRecord,
    Record,
    ServiceRecord,
    UserRecord,
)

__all__ = ["Session", "SessionError", "SessionState"]

R = TypeVar("R", bound=Record)


class SessionError(Exception):
    """Нарушен порядок записей сессии."""


class SessionState(StrEnum):
    """В чём сессия сейчас: переходы — раздел 5.18 плана."""

    IDLE = "idle"
    TURN = "turn"
    WAITING = "waiting"
    COMPACTING = "compacting"
    STOPPING = "stopping"


class Session:
    """Список записей сессии с выдачей seq и проекциями над ним."""

    def __init__(self, session_id: SessionId) -> None:
        self._id = session_id
        self._records: list[Record] = []
        self._by_uuid: dict[str, ChainRecord] = {}
        self._state = SessionState.IDLE
        self._conversation: list[ChainRecord] | None = None

    @property
    def id(self) -> SessionId:
        return self._id

    @property
    def state(self) -> SessionState:
        return self._state

    def enter(self, state: SessionState) -> None:
        self._state = state

    @property
    def last_seq(self) -> int:
        if not self._records:
            return 0

        return self._records[-1].seq

    def restore(self, records: Iterable[Record]) -> None:
        """Принимает записи из журнала с их seq; порядок обязан расти."""
        for record in records:
            if record.seq <= self.last_seq:
                msg = (
                    f"Cannot restore session '{self._id.value}': record seq "
                    f"{record.seq} comes after seq {self.last_seq}, but records must "
                    "arrive in increasing order. The journal is out of order."
                )
                raise SessionError(msg)

            self._place(record)

    def add(self, record: R) -> R:
        """Выдаёт записи следующий seq и добавляет её; возвращает копию с seq."""
        if record.seq != 0:
            msg = (
                f"Cannot add a '{record.type.value}' record to session "
                f"'{self._id.value}': it already carries seq {record.seq}. New "
                "records come without a seq; the session assigns it."
            )
            raise SessionError(msg)

        sequenced = record.model_copy(update={"seq": self.last_seq + 1})
        self._place(sequenced)

        return sequenced

    def records(self) -> Sequence[Record]:
        return self._records

    def since(self, seq: int) -> Sequence[Record]:
        """Записи с seq больше указанного: несброшенные для писателя."""
        tail: list[Record] = []
        for record in self._records:
            if record.seq > seq:
                tail.append(record)

        return tail

    def chain(self) -> Sequence[ChainRecord]:
        """Все записи цепочки в порядке seq."""
        return list(self._chain_records())

    def service(self) -> Sequence[ServiceRecord]:
        """Все служебные записи в порядке seq."""
        tail: list[ServiceRecord] = []
        for record in self._records:
            if isinstance(record, ServiceRecord):
                tail.append(record)

        return tail

    def find(self, uuid: str) -> ChainRecord | None:
        return self._by_uuid.get(uuid)

    def last_chain_uuid(self) -> str | None:
        """uuid последней записи цепочки — родитель следующей."""
        for record in reversed(self._records):
            if isinstance(record, ChainRecord):
                return record.uuid

        return None

    def conversation(self) -> Sequence[ChainRecord]:
        """Разговор: записи от последней полной границы сжатия с перенесённым
        хвостом сразу за пересказом; без границы — вся цепочка.

        Полная граница — та, за которой лежит её пересказ (anchorUuid из
        preservedMessages); граница без пересказа — след упавшего сжатия,
        она не разрывает разговор. Записи хвоста несут обнулённый учёт.
        """
        if self._conversation is None:
            self._conversation = list(self._build_conversation())

        return self._conversation

    def _place(self, record: Record) -> None:
        self._records.append(record)
        if isinstance(record, ChainRecord):
            self._by_uuid[record.uuid] = record

        self._conversation = None

    def _chain_records(self) -> Iterator[ChainRecord]:
        for record in self._records:
            if isinstance(record, ChainRecord):
                yield record

    def _build_conversation(self) -> Iterator[ChainRecord]:
        chain = self.chain()
        boundary_at = self._last_complete_boundary(chain)
        if boundary_at is None:
            yield from chain
            return

        boundary = chain[boundary_at]
        if not isinstance(boundary, CompactBoundaryRecord):
            yield from chain
            return

        anchor = boundary.compact_metadata.preserved_messages.anchor_uuid
        index = boundary_at
        while index < len(chain):
            record = chain[index]
            yield record
            index += 1
            if record.uuid == anchor:
                break

        yield from self._preserved_tail(boundary)
        yield from chain[index:]

    def _preserved_tail(self, boundary: CompactBoundaryRecord) -> Iterator[ChainRecord]:
        for uuid in boundary.compact_metadata.preserved_messages.uuids:
            record = self._by_uuid.get(uuid)
            if record is None:
                continue

            if isinstance(record, AssistantRecord):
                yield record.with_zero_usage()
                continue

            yield record

    def _last_complete_boundary(self, chain: Sequence[ChainRecord]) -> int | None:
        summaries: set[str] = set()
        for record in chain:
            if isinstance(record, UserRecord) and record.is_compact_summary:
                summaries.add(record.uuid)

        for index in range(len(chain) - 1, -1, -1):
            record = chain[index]
            if not isinstance(record, CompactBoundaryRecord):
                continue

            anchor = record.compact_metadata.preserved_messages.anchor_uuid
            if anchor in summaries:
                return index

        return None
