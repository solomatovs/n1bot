"""Сравнение двух снимков каталога по сущностям.

CatalogDiff.between(base, other) даёт статус каждой сущности по id;
неизменённые в entries не пишутся, status_of отвечает за них UNCHANGED.
Этим diff'ом страница подсвечивает черновик относительно опубликованного.
"""

from __future__ import annotations

from collections.abc import Iterator

from boba.catalog.base import CatalogModel, ChangeStatus
from boba.catalog.model import CatalogSnapshot, EntityKind, EntityRef

__all__ = [
    "CatalogDiff",
    "ChangeStatus",
    "DiffEntry",
]


class DiffEntry(CatalogModel):
    ref: EntityRef
    status: ChangeStatus


class CatalogDiff(CatalogModel):
    """Изменения между двумя снимками; только затронутые сущности."""

    entries: tuple[DiffEntry, ...]

    @classmethod
    def between(cls, base: CatalogSnapshot, other: CatalogSnapshot) -> CatalogDiff:
        entries = list(cls._entries(base, other))
        return cls(entries=tuple(entries))

    def status_of(self, ref: EntityRef) -> ChangeStatus:
        for entry in self.entries:
            if entry.ref != ref:
                continue

            return entry.status

        return ChangeStatus.UNCHANGED

    def is_empty(self) -> bool:
        return not self.entries

    @classmethod
    def _entries(
        cls, base: CatalogSnapshot, other: CatalogSnapshot
    ) -> Iterator[DiffEntry]:
        for kind in EntityKind:
            before = base.table(kind)
            after = other.table(kind)

            for entity_id, entity in before.items():
                ref = EntityRef(kind=kind, id=entity_id)
                replacement = after.get(entity_id)
                if replacement is None:
                    yield DiffEntry(ref=ref, status=ChangeStatus.REMOVED)
                    continue

                if replacement.model_dump() == entity.model_dump():
                    continue

                yield DiffEntry(ref=ref, status=ChangeStatus.MODIFIED)

            for entity_id in after:
                if entity_id in before:
                    continue

                ref = EntityRef(kind=kind, id=entity_id)
                yield DiffEntry(ref=ref, status=ChangeStatus.ADDED)
