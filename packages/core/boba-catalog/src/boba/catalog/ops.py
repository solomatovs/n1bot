"""Операции над снимком процесса: по три на группу, узел и поток, плюс
перенацеливание узла на другой адрес; позиция и группа узла меняются
set_node. Список операций разбирается из JSON
на границе и применяется к снимку по одной с проверкой инвариантов после
каждой; ссылки на объекты и колонки подключений проверяются по резолверу,
который даёт сервис.

Ошибки:
CatalogOpError — операция не применима; index и op называют её, reason —
    причину из инвариантов.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, RootModel

from boba.catalog.base import CatalogError, CatalogInvariantError, CatalogModel
from boba.catalog.model import (
    CatalogSnapshot,
    EntityKind,
    EntityRef,
    Flow,
    Group,
    Node,
    ObjectResolver,
)
from boba.catalog.sources import ObjectRef

__all__ = [
    "AcceptAll",
    "AddFlow",
    "AddGroup",
    "AddNode",
    "CatalogOp",
    "CatalogOpBase",
    "CatalogOpError",
    "CatalogOpKind",
    "OperationList",
    "RemoveFlow",
    "RemoveGroup",
    "RemoveNode",
    "RetargetNode",
    "SetFlow",
    "SetGroup",
    "SetNode",
]


class CatalogOpKind(StrEnum):
    ADD_GROUP = "add_group"
    SET_GROUP = "set_group"
    REMOVE_GROUP = "remove_group"
    ADD_NODE = "add_node"
    SET_NODE = "set_node"
    REMOVE_NODE = "remove_node"
    RETARGET_NODE = "retarget_node"
    ADD_FLOW = "add_flow"
    SET_FLOW = "set_flow"
    REMOVE_FLOW = "remove_flow"


class CatalogOpBase(CatalogModel, ABC):
    """Базовая операция; наследники — по одной на пару действие и сущность.

    Каждая операция умеет применить себя к снимку и вернуть новый; проверку
    инвариантов после применения делает OperationList.
    """

    op: CatalogOpKind

    @abstractmethod
    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        """Новый снимок с применённой операцией.

        Ошибки:
        CatalogInvariantError — операция не применима; причина в сообщении.
        """


class CatalogOpError(CatalogError):
    """Операция из списка не применима; index — её номер в списке."""

    def __init__(self, index: int, op: CatalogOpBase, reason: str) -> None:
        self.index = index
        self.op = op
        self.reason = reason
        super().__init__(f"operation #{index} ({op.op.value}): {reason}")


class AddGroup(CatalogOpBase):
    op: Literal[CatalogOpKind.ADD_GROUP] = CatalogOpKind.ADD_GROUP
    group: Group

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        return snapshot.added(self.group)


class SetGroup(CatalogOpBase):
    op: Literal[CatalogOpKind.SET_GROUP] = CatalogOpKind.SET_GROUP
    group: Group

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        return snapshot.replaced(self.group)


class RemoveGroup(CatalogOpBase):
    """Удаление группы; отказывает, пока в ней состоят узлы, и называет их."""

    op: Literal[CatalogOpKind.REMOVE_GROUP] = CatalogOpKind.REMOVE_GROUP
    id: UUID

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        ref = EntityRef(kind=EntityKind.GROUP, id=self.id)

        held: list[str] = []
        for node in snapshot.nodes_in(self.id):
            held.append(node.label)

        if held:
            label = snapshot.label(ref)
            msg = (
                f"{label} still holds {len(held)} node(s): {held}; move them out first"
            )
            raise CatalogInvariantError([msg])

        return snapshot.removed(ref)


class AddNode(CatalogOpBase):
    op: Literal[CatalogOpKind.ADD_NODE] = CatalogOpKind.ADD_NODE
    node: Node

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        return snapshot.added(self.node)


class SetNode(CatalogOpBase):
    """Замена узла целиком: позиция, группа, псевдоним, заметка; адрес меняет
    RetargetNode."""

    op: Literal[CatalogOpKind.SET_NODE] = CatalogOpKind.SET_NODE
    node: Node

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        current = snapshot.nodes.get(self.node.id)
        if current is not None and current.ref != self.node.ref:
            label = snapshot.label(EntityRef.of(current))
            msg = f"{label}: use retarget_node to change the object address"
            raise CatalogInvariantError([msg])

        return snapshot.replaced(self.node)


class RetargetNode(CatalogOpBase):
    """Узел указывает на другой объект: например, на ту же таблицу другого
    подключения. Потоки узла остаются."""

    op: Literal[CatalogOpKind.RETARGET_NODE] = CatalogOpKind.RETARGET_NODE
    id: UUID
    ref: ObjectRef

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        entity = EntityRef(kind=EntityKind.NODE, id=self.id)
        current = snapshot.nodes.get(self.id)
        if current is None:
            msg = f"{snapshot.label(entity)} not found in the catalog"
            raise CatalogInvariantError([msg])

        return snapshot.replaced(current.model_copy(update={"ref": self.ref}))


class RemoveNode(CatalogOpBase):
    """Удаление узла; отказывает, пока на узел ссылается поток."""

    op: Literal[CatalogOpKind.REMOVE_NODE] = CatalogOpKind.REMOVE_NODE
    id: UUID

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        ref = EntityRef(kind=EntityKind.NODE, id=self.id)

        flows = list(snapshot.flows_of(self.id))
        if flows:
            label = snapshot.label(ref)
            msg = f"{label} is used by {len(flows)} flow(s); remove them first"
            raise CatalogInvariantError([msg])

        return snapshot.removed(ref)


class AddFlow(CatalogOpBase):
    op: Literal[CatalogOpKind.ADD_FLOW] = CatalogOpKind.ADD_FLOW
    flow: Flow

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        return snapshot.added(self.flow)


class SetFlow(CatalogOpBase):
    op: Literal[CatalogOpKind.SET_FLOW] = CatalogOpKind.SET_FLOW
    flow: Flow

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        return snapshot.replaced(self.flow)


class RemoveFlow(CatalogOpBase):
    op: Literal[CatalogOpKind.REMOVE_FLOW] = CatalogOpKind.REMOVE_FLOW
    id: UUID

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        return snapshot.removed(EntityRef(kind=EntityKind.FLOW, id=self.id))


CatalogOp = (
    AddGroup
    | SetGroup
    | RemoveGroup
    | AddNode
    | SetNode
    | RetargetNode
    | RemoveNode
    | AddFlow
    | SetFlow
    | RemoveFlow
)


class AcceptAll(ObjectResolver):
    """Резолвер без знаний о снимках: любой объект существует, колонки
    неизвестны. Для мест, где снимки не нужны: тесты домена, свёртка
    истории версий."""

    def exists(self, ref: ObjectRef) -> bool:
        return True

    def columns_of(self, ref: ObjectRef) -> None:
        return None


class OperationList(RootModel[tuple[CatalogOp, ...]]):
    """Список операций: разбор JSON на границе и применение к снимку.

    Разбор — model_validate / model_validate_json, обратно — model_dump_json.
    Пустой список применяется и возвращает тот же снимок.
    """

    model_config = ConfigDict(frozen=True)

    def apply(
        self, snapshot: CatalogSnapshot, resolver: ObjectResolver
    ) -> CatalogSnapshot:
        """Новый снимок после всех операций; входной не меняется.

        Ссылки на снимки проверяются по разнице: операция отвергается, только
        если добавляет новое расхождение со снимками; уже устаревший процесс
        можно чинить по одной операции.

        Ошибки:
        CatalogOpError — первая не применимая операция, дальше не идём.
        """
        current = snapshot
        known = set(current.source_violations(resolver))
        for index, op in enumerate(self.root):
            try:
                current = op.apply_to(current)
                current.check()
            except CatalogInvariantError as exc:
                raise CatalogOpError(index, op, str(exc)) from exc

            violations = set(current.source_violations(resolver))
            introduced = sorted(violations - known)
            if introduced:
                raise CatalogOpError(index, op, "; ".join(introduced))

            known = violations

        return current
