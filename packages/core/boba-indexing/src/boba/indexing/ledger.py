"""Реестр источников: что уже в индексе, с каким отпечатком и когда его видели.

Реестр — память конвейера между прогонами. Источник попадает сюда после
индексации вместе с отпечатком версии и хэшем тела; следующий прогон по
отпечатку решает, нужно ли вообще скачивать и разбирать источник, а по
отметке seen_at после обхода находит то, чего в источнике данных больше нет.

Ошибки:
LedgerError — хранилище реестра недоступно или ответило не тем, что ждали.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from boba.indexing.errors import IndexingError
from boba.indexing.sections import SourceId

__all__ = [
    "ChangePolicy",
    "LedgerError",
    "NoProbe",
    "SourceLedger",
    "SourceMark",
    "SourceProbe",
    "SourceRecord",
]


class LedgerError(IndexingError):
    """Реестр источников недоступен: ошибка хранилища, а не источника.

    Не изолируется по источнику — без реестра прогон бессмыслен.
    """


@dataclass(frozen=True)
class SourceMark:
    """Что источник знает о себе до скачивания.

    fingerprint — отпечаток версии из списка источников (номер версии, дата,
    размер); совпал с реестром — источник не изменился. grade — уровень
    разбора, которого просит прогон: запись с меньшим уровнем считается
    устаревшей, с большим не откатывается. parent — источник, в списке
    которого этот числится; его отсутствие в списке живого родителя значит
    удаление.
    """

    fingerprint: str
    grade: int = 0
    parent: SourceId | None = None


@dataclass(frozen=True)
class SourceRecord:
    """Запись реестра об одном проиндексированном источнике."""

    source_id: SourceId
    parent: SourceId | None
    fingerprint: str
    content_hash: str
    grade: int
    stamp: str
    seen_at: float
    indexed_at: float


class SourceLedger(ABC):
    """Порт реестра источников одной коллекции.

    Конвейер зовёт lookup перед скачиванием, touch для увиденных без
    изменений, record после индексации и unseen/children/forget в очистке.
    Реализация хранит записи рядом с чанками (PostgresSourceLedger).
    """

    @abstractmethod
    async def lookup(self, source_id: SourceId) -> SourceRecord | None:
        """Запись источника; None, если он ещё не индексировался."""
        ...

    @abstractmethod
    async def touch(self, source_ids: Sequence[SourceId], *, at: float) -> None:
        """Отметить источники увиденными; неизвестные реестру пропускаются."""
        ...

    @abstractmethod
    async def record(self, record: SourceRecord) -> None:
        """Записать или заменить запись источника."""
        ...

    @abstractmethod
    def unseen(self, *, before: float) -> AsyncIterator[SourceRecord]:
        """Источники, которых не видели с момента before; потоком, без списка."""
        ...

    @abstractmethod
    def children(self, parent: SourceId) -> AsyncIterator[SourceRecord]:
        """Источники, числящиеся за родителем."""
        ...

    @abstractmethod
    async def forget(self, source_id: SourceId) -> None:
        """Удалить запись источника; чанки удаляет IndexSink."""
        ...


class SourceProbe(ABC):
    """Проверка существования корневых источников, не попавших в обход.

    Обход видит только то, что источник данных вернул; исчезнувшую страницу
    он не сообщит. Реализация спрашивает источник данных пакетом и отдаёт
    id тех, кого там больше нет.
    """

    @abstractmethod
    async def gone(self, records: Sequence[SourceRecord]) -> Sequence[SourceId]: ...


class NoProbe(SourceProbe):
    """Прогон, который ничего не удаляет за пределами увиденных родителей."""

    async def gone(self, records: Sequence[SourceRecord]) -> Sequence[SourceId]:
        return ()


class ChangePolicy:
    """Правило, когда источник можно не скачивать и не разбирать."""

    @staticmethod
    def unchanged(record: SourceRecord | None, mark: SourceMark, stamp: str) -> bool:
        """Отпечаток, штамп конвейера и уровень разбора совпали: не качать."""
        if record is None:
            return False

        if record.stamp != stamp:
            return False

        if record.grade < mark.grade:
            return False

        return record.fingerprint == mark.fingerprint

    @staticmethod
    def same_body(
        record: SourceRecord | None,
        mark: SourceMark,
        stamp: str,
        body_hash: str,
    ) -> bool:
        """Тело скачано и совпало байт в байт с разобранным ранее: не разбирать."""
        if record is None:
            return False

        if not body_hash:
            return False

        if record.stamp != stamp:
            return False

        if record.grade < mark.grade:
            return False

        return record.content_hash == body_hash
