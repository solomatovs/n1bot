"""Операции над снимком процесса: по три на слой, узел, вид загрузки и поток,
плюс перенацеливание узла на другой адрес. Список операций разбирается из
JSON на границе и применяется к снимку по одной с проверкой инвариантов
после каждой; ссылки на объекты и колонки источников проверяются по
резолверу, который даёт сервис.

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
    Layer,
    LoadKind,
    Node,
    ObjectResolver,
)
from boba.catalog.sources import ObjectRef

__all__ = [
    "AcceptAll",
    "AddFlow",
    "AddLayer",
    "AddLoadKind",
    "AddNode",
    "CatalogOp",
    "CatalogOpBase",
    "CatalogOpError",
    "CatalogOpKind",
    "OperationList",
    "RemoveFlow",
    "RemoveLayer",
    "RemoveLoadKind",
    "RemoveNode",
    "RetargetNode",
    "SetFlow",
    "SetLayer",
    "SetLoadKind",
    "SetNode",
]


class CatalogOpKind(StrEnum):
    ADD_LAYER = "add_layer"
    SET_LAYER = "set_layer"
    REMOVE_LAYER = "remove_layer"
    ADD_NODE = "add_node"
    SET_NODE = "set_node"
    REMOVE_NODE = "remove_node"
    RETARGET_NODE = "retarget_node"
    ADD_LOAD_KIND = "add_load_kind"
    SET_LOAD_KIND = "set_load_kind"
    REMOVE_LOAD_KIND = "remove_load_kind"
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


class AddLayer(CatalogOpBase):
    op: Literal[CatalogOpKind.ADD_LAYER] = CatalogOpKind.ADD_LAYER
    layer: Layer

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        return snapshot.added(self.layer)


class SetLayer(CatalogOpBase):
    op: Literal[CatalogOpKind.SET_LAYER] = CatalogOpKind.SET_LAYER
    layer: Layer

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        return snapshot.replaced(self.layer)


class RemoveLayer(CatalogOpBase):
    """Удаление слоя; отказывает, пока в слое есть узлы."""

    op: Literal[CatalogOpKind.REMOVE_LAYER] = CatalogOpKind.REMOVE_LAYER
    id: UUID

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        ref = EntityRef(kind=EntityKind.LAYER, id=self.id)

        held = list(snapshot.nodes_in(self.id))
        if held:
            label = snapshot.label(ref)
            msg = f"{label} still holds {len(held)} node(s); remove them first"
            raise CatalogInvariantError([msg])

        return snapshot.removed(ref)


class AddNode(CatalogOpBase):
    op: Literal[CatalogOpKind.ADD_NODE] = CatalogOpKind.ADD_NODE
    node: Node

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        return snapshot.added(self.node)


class SetNode(CatalogOpBase):
    """Замена узла целиком: слой, псевдоним, заметка; адрес меняет RetargetNode."""

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
    """Узел указывает на другой объект: например, с ручного источника на
    реальный. Потоки узла остаются."""

    op: Literal[CatalogOpKind.RETARGET_NODE] = CatalogOpKind.RETARGET_NODE
    id: UUID
    ref: ObjectRef

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        entity = EntityRef(kind=EntityKind.NODE, id=self.id)
        current = snapshot.nodes.get(self.id)
        if current is None:
            msg = f"{snapshot.label(entity)} not found"
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


class AddLoadKind(CatalogOpBase):
    op: Literal[CatalogOpKind.ADD_LOAD_KIND] = CatalogOpKind.ADD_LOAD_KIND
    load_kind: LoadKind

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        return snapshot.added(self.load_kind)


class SetLoadKind(CatalogOpBase):
    op: Literal[CatalogOpKind.SET_LOAD_KIND] = CatalogOpKind.SET_LOAD_KIND
    load_kind: LoadKind

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        return snapshot.replaced(self.load_kind)


class RemoveLoadKind(CatalogOpBase):
    """Удаление вида; отказывает, пока есть потоки этого вида."""

    op: Literal[CatalogOpKind.REMOVE_LOAD_KIND] = CatalogOpKind.REMOVE_LOAD_KIND
    id: UUID

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        ref = EntityRef(kind=EntityKind.LOAD_KIND, id=self.id)

        flows = list(snapshot.flows_of_kind(self.id))
        if flows:
            label = snapshot.label(ref)
            msg = f"{label} is used by {len(flows)} flow(s); change them first"
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
    AddLayer
    | SetLayer
    | RemoveLayer
    | AddNode
    | SetNode
    | RetargetNode
    | RemoveNode
    | AddLoadKind
    | SetLoadKind
    | RemoveLoadKind
    | AddFlow
    | SetFlow
    | RemoveFlow
)


class AcceptAll(ObjectResolver):
    """Резолвер без знаний об источниках: любой объект существует, колонки
    неизвестны. Для мест, где источники не нужны: тесты домена, свёртка
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

        Ссылки на источники проверяются по разнице: операция отвергается,
        только если добавляет новое расхождение с источниками; уже устаревший
        процесс можно чинить по одной операции.

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
