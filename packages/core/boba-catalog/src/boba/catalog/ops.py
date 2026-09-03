"""Операции над снимком каталога и их применение списком.

Список операций это единица правки: порция черновика, тело версии, запрос
инструмента LLM. OperationList разбирает JSON на границе и применяет
операции к снимку по одной, после каждой проверяя инварианты; снимок на
входе не меняется, наружу выходит новый.

Ошибки:
CatalogOpError — операция из списка не применима: занят или не найден id,
    остались зависимые сущности, нарушен инвариант; index — номер в списке.
CatalogInvariantError — та же причина у одиночной операции, применённой
    напрямую через apply_to.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, RootModel

from boba.catalog.model import (
    CatalogError,
    CatalogInvariantError,
    CatalogModel,
    CatalogSnapshot,
    Column,
    Dataset,
    EntityKind,
    EntityRef,
    Flow,
    Layer,
    LoadKind,
)

__all__ = [
    "AddColumn",
    "AddDataset",
    "AddFlow",
    "AddLayer",
    "AddLoadKind",
    "CatalogOp",
    "CatalogOpBase",
    "CatalogOpError",
    "CatalogOpKind",
    "OperationList",
    "RemoveColumn",
    "RemoveDataset",
    "RemoveFlow",
    "RemoveLayer",
    "RemoveLoadKind",
    "SetColumn",
    "SetDataset",
    "SetFlow",
    "SetLayer",
    "SetLoadKind",
]


class CatalogOpKind(StrEnum):
    ADD_LAYER = "add_layer"
    SET_LAYER = "set_layer"
    REMOVE_LAYER = "remove_layer"
    ADD_DATASET = "add_dataset"
    SET_DATASET = "set_dataset"
    REMOVE_DATASET = "remove_dataset"
    ADD_COLUMN = "add_column"
    SET_COLUMN = "set_column"
    REMOVE_COLUMN = "remove_column"
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
        CatalogInvariantError — операция не применима к этому снимку.
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
    """Удаление слоя; отказывает, пока в слое есть наборы."""

    op: Literal[CatalogOpKind.REMOVE_LAYER] = CatalogOpKind.REMOVE_LAYER
    id: UUID

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        ref = EntityRef(kind=EntityKind.LAYER, id=self.id)

        held = list(snapshot.datasets_in(self.id))
        if held:
            label = snapshot.label(ref)
            msg = f"{label} still holds {len(held)} dataset(s); remove them first"
            raise CatalogInvariantError([msg])

        return snapshot.removed(ref)


class AddDataset(CatalogOpBase):
    op: Literal[CatalogOpKind.ADD_DATASET] = CatalogOpKind.ADD_DATASET
    dataset: Dataset

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        return snapshot.added(self.dataset)


class SetDataset(CatalogOpBase):
    op: Literal[CatalogOpKind.SET_DATASET] = CatalogOpKind.SET_DATASET
    dataset: Dataset

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        return snapshot.replaced(self.dataset)


class RemoveDataset(CatalogOpBase):
    """Удаление набора с его колонками; отказывает, пока на набор ссылается поток."""

    op: Literal[CatalogOpKind.REMOVE_DATASET] = CatalogOpKind.REMOVE_DATASET
    id: UUID

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        ref = EntityRef(kind=EntityKind.DATASET, id=self.id)

        flows = list(snapshot.flows_of(self.id))
        if flows:
            label = snapshot.label(ref)
            msg = f"{label} is used by {len(flows)} flow(s); remove them first"
            raise CatalogInvariantError([msg])

        current = snapshot.removed(ref)
        for column in snapshot.columns_of(self.id):
            current = current.removed(EntityRef.of(column))

        return current


class AddColumn(CatalogOpBase):
    op: Literal[CatalogOpKind.ADD_COLUMN] = CatalogOpKind.ADD_COLUMN
    column: Column

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        return snapshot.added(self.column)


class SetColumn(CatalogOpBase):
    op: Literal[CatalogOpKind.SET_COLUMN] = CatalogOpKind.SET_COLUMN
    column: Column

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        return snapshot.replaced(self.column)


class RemoveColumn(CatalogOpBase):
    """Удаление колонки; отказывает, пока колонка упомянута в значениях потока."""

    op: Literal[CatalogOpKind.REMOVE_COLUMN] = CatalogOpKind.REMOVE_COLUMN
    id: UUID

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        ref = EntityRef(kind=EntityKind.COLUMN, id=self.id)

        flows = list(snapshot.flows_using_column(self.id))
        if flows:
            label = snapshot.label(ref)
            msg = f"{label} is referenced by {len(flows)} flow(s); change them first"
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
    """Удаление вида загрузки; отказывает, пока есть потоки этого вида."""

    op: Literal[CatalogOpKind.REMOVE_LOAD_KIND] = CatalogOpKind.REMOVE_LOAD_KIND
    id: UUID

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        ref = EntityRef(kind=EntityKind.LOAD_KIND, id=self.id)

        flows = list(snapshot.flows_of_kind(self.id))
        if flows:
            label = snapshot.label(ref)
            msg = f"{label} is used by {len(flows)} flow(s); remove them first"
            raise CatalogInvariantError([msg])

        return snapshot.removed(ref)


class AddFlow(CatalogOpBase):
    op: Literal[CatalogOpKind.ADD_FLOW] = CatalogOpKind.ADD_FLOW
    flow: Flow

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        flow = snapshot.conformed(self.flow)
        return snapshot.added(flow)


class SetFlow(CatalogOpBase):
    op: Literal[CatalogOpKind.SET_FLOW] = CatalogOpKind.SET_FLOW
    flow: Flow

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        flow = snapshot.conformed(self.flow)
        return snapshot.replaced(flow)


class RemoveFlow(CatalogOpBase):
    op: Literal[CatalogOpKind.REMOVE_FLOW] = CatalogOpKind.REMOVE_FLOW
    id: UUID

    def apply_to(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        ref = EntityRef(kind=EntityKind.FLOW, id=self.id)
        return snapshot.removed(ref)


CatalogOp = Annotated[
    AddLayer
    | SetLayer
    | RemoveLayer
    | AddDataset
    | SetDataset
    | RemoveDataset
    | AddColumn
    | SetColumn
    | RemoveColumn
    | AddLoadKind
    | SetLoadKind
    | RemoveLoadKind
    | AddFlow
    | SetFlow
    | RemoveFlow,
    Field(discriminator="op"),
]


class OperationList(RootModel[tuple[CatalogOp, ...]]):
    """Список операций: разбор JSON на границе и применение к снимку.

    Разбор — model_validate / model_validate_json, обратно — model_dump_json.
    Пустой список применяется и возвращает тот же снимок.
    """

    model_config = ConfigDict(frozen=True)

    def apply(self, snapshot: CatalogSnapshot) -> CatalogSnapshot:
        """Новый снимок после всех операций; входной не меняется.

        Ошибки:
        CatalogOpError — первая не применимая операция, дальше не идём.
        """
        current = snapshot
        for index, op in enumerate(self.root):
            try:
                current = op.apply_to(current)
                current.check()
            except CatalogInvariantError as exc:
                raise CatalogOpError(index, op, str(exc)) from exc

        return current
