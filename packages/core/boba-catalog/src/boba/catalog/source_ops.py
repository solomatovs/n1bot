"""Операции ручного источника: объект с коротким набором полей (имя, вид,
комментарий, колонки с типом, nullable и комментарием) добавляется, заменяется
или удаляется в снимке. Как объект ложится в родную структуру, знает сам
снимок (SourceSnapshot.with_object / without_object).

Ошибки:
SourceOpError — операция не применима: объекта нет, объект уже есть, путь
    не той длины для вида источника.
CatalogInvariantError — снимок после операции нарушает инварианты.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Literal

from pydantic import Field, RootModel

from boba.catalog.base import CatalogError, CatalogInvariantError, CatalogModel
from boba.catalog.sources import ManualObject, SourceSnapshot

__all__ = [
    "AddObject",
    "RemoveObject",
    "SetObject",
    "SourceOp",
    "SourceOpError",
    "SourceOpKind",
    "SourceOperationList",
]


class SourceOpKind(StrEnum):
    ADD_OBJECT = "add_object"
    SET_OBJECT = "set_object"
    REMOVE_OBJECT = "remove_object"


class SourceOpError(CatalogError):
    """Операция из списка не применима; index — её номер в списке."""

    def __init__(self, index: int, op: SourceOpBase, reason: str) -> None:
        self.index = index
        self.op = op
        self.reason = reason
        super().__init__(f"operation #{index} ({op.op.value}): {reason}")


class SourceOpBase(CatalogModel, ABC):
    op: SourceOpKind

    @abstractmethod
    def apply_to(self, snapshot: SourceSnapshot) -> SourceSnapshot:
        """Новый снимок с применённой операцией.

        Ошибки:
        CatalogInvariantError — операция не применима; причина в сообщении.
        """


class AddObject(SourceOpBase):
    op: Literal[SourceOpKind.ADD_OBJECT] = SourceOpKind.ADD_OBJECT
    object: ManualObject

    def apply_to(self, snapshot: SourceSnapshot) -> SourceSnapshot:
        if snapshot.has_manual_object(self.object.path):
            rendered = "/".join(self.object.path)
            kind = snapshot.kind
            msg = f"object {rendered} already exists in the {kind} snapshot"
            raise CatalogInvariantError([msg])

        return snapshot.with_object(self.object)


class SetObject(SourceOpBase):
    op: Literal[SourceOpKind.SET_OBJECT] = SourceOpKind.SET_OBJECT
    object: ManualObject

    def apply_to(self, snapshot: SourceSnapshot) -> SourceSnapshot:
        if not snapshot.has_manual_object(self.object.path):
            rendered = "/".join(self.object.path)
            kind = snapshot.kind
            msg = f"object {rendered} not found in the {kind} snapshot"
            raise CatalogInvariantError([msg])

        without = snapshot.without_object(self.object.path)
        return without.with_object(self.object)


class RemoveObject(SourceOpBase):
    op: Literal[SourceOpKind.REMOVE_OBJECT] = SourceOpKind.REMOVE_OBJECT
    path: tuple[str, ...] = Field(min_length=2)

    def apply_to(self, snapshot: SourceSnapshot) -> SourceSnapshot:
        if not snapshot.has_manual_object(self.path):
            rendered = "/".join(self.path)
            kind = snapshot.kind
            msg = f"object {rendered} not found in the {kind} snapshot"
            raise CatalogInvariantError([msg])

        return snapshot.without_object(self.path)


SourceOp = AddObject | SetObject | RemoveObject


class SourceOperationList(RootModel[tuple[SourceOp, ...]]):
    """Список операций ручного источника: разбор JSON на границе и применение
    к снимку с проверкой инвариантов после каждой операции."""

    def apply(self, snapshot: SourceSnapshot) -> SourceSnapshot:
        """Ошибки:
        SourceOpError — первая не применимая операция, дальше не идём.
        """
        current = snapshot
        for index, op in enumerate(self.root):
            try:
                current = op.apply_to(current)
                current.check()
            except CatalogInvariantError as exc:
                raise SourceOpError(index, op, str(exc)) from exc

        return current
