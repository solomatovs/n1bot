"""Домен каталога данных: сущности, снимок версии и его инварианты.

Снимок это полное состояние каталога одной версии: словари слоёв, наборов,
колонок, видов загрузки и потоков по id. Он неизменяем: операции из
boba.catalog.ops получают новый снимок методами added/replaced/removed и
после каждой операции зовут check(). Хранилище зовёт check() после сборки
снимка из строк таблиц.

Ошибки:
CatalogInvariantError — снимок или значения потока нарушают инварианты,
    перечень нарушений в violations.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Hashable, Iterable, Iterator, Mapping, Sequence
from enum import StrEnum
from typing import TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "CatalogEntity",
    "CatalogError",
    "CatalogInvariantError",
    "CatalogModel",
    "CatalogSnapshot",
    "Column",
    "Dataset",
    "EntityKind",
    "EntityRef",
    "Flow",
    "Layer",
    "LoadField",
    "LoadFieldType",
    "LoadKind",
    "LoadSpec",
    "LoadValue",
]

LoadValue = str | int | bool | UUID | tuple[UUID, ...]

KeyT = TypeVar("KeyT", bound=Hashable)


class CatalogError(Exception):
    """Базовая ошибка домена; наследники — CatalogInvariantError и CatalogOpError."""


class CatalogInvariantError(CatalogError):
    """Снимок нарушает инварианты; каждое нарушение отдельной строкой."""

    def __init__(self, violations: Sequence[str]) -> None:
        self.violations = tuple(violations)
        text = "; ".join(self.violations)
        super().__init__(text)


class CatalogModel(BaseModel):
    """Базовая модель домена: неизменяемая, лишние ключи запрещены."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class LoadFieldType(StrEnum):
    """Тип поля вида загрузки; знает форму хранения значения и ссылки на колонки."""

    TEXT = "text"
    INT = "int"
    BOOL = "bool"
    COLUMN = "column"
    COLUMNS = "columns"

    def accepts(self, value: LoadValue) -> bool:
        """Значение имеет форму хранения этого типа."""
        if self is LoadFieldType.TEXT:
            return isinstance(value, str)

        if self is LoadFieldType.INT:
            return self._is_int(value)

        if self is LoadFieldType.BOOL:
            return isinstance(value, bool)

        if self is LoadFieldType.COLUMN:
            return isinstance(value, UUID)

        return self._is_columns(value)

    @staticmethod
    def _is_int(value: LoadValue) -> bool:
        if isinstance(value, bool):
            return False

        return isinstance(value, int)

    @staticmethod
    def _is_columns(value: LoadValue) -> bool:
        if not isinstance(value, tuple):
            return False

        return len(value) > 0

    def column_ids(self, value: LoadValue) -> tuple[UUID, ...]:
        """Ссылки на колонки внутри значения; у скалярных типов пусто."""
        if self is LoadFieldType.COLUMN:
            if isinstance(value, UUID):
                return (value,)

            return ()

        if self is LoadFieldType.COLUMNS:
            if isinstance(value, tuple):
                return value

            return ()

        return ()

    def conform(self, value: LoadValue) -> LoadValue:
        """Значение из JSON в форму хранения: ссылка на колонку строкой становится UUID.

        Ошибки:
        ValueError — строка не разбирается как UUID.
        """
        if self is not LoadFieldType.COLUMN:
            return value

        if not isinstance(value, str):
            return value

        return UUID(value)

    @staticmethod
    def shape_of(value: LoadValue) -> str:
        """Форма значения для текста нарушения."""
        if isinstance(value, tuple):
            return f"list of {len(value)}"

        return type(value).__name__


class LoadField(CatalogModel):
    """Поле вида загрузки: имя, тип и обязательность."""

    name: str = Field(min_length=1)
    type: LoadFieldType
    required: bool
    description: str = ""


class LoadKind(CatalogModel):
    """Вид загрузки, заведённый пользователем: имя и описание полей.

    Поток хранит значения по именам полей вида; вид проверяет их состав и
    типы (violations_of) и приводит ссылки на колонки из JSON к UUID (conform).
    """

    id: UUID
    name: str = Field(min_length=1)
    description: str = ""
    fields: tuple[LoadField, ...]

    @model_validator(mode="after")
    def _unique_field_names(self) -> LoadKind:
        names: list[str] = []
        for field in self.fields:
            names.append(field.name)

        repeated = sorted(set(CatalogSnapshot.repeated(names)))
        if repeated:
            msg = f"load kind {self.name!r}: duplicate field names {repeated}"
            raise ValueError(msg)

        return self

    def field_names(self) -> tuple[str, ...]:
        names: list[str] = []
        for field in self.fields:
            names.append(field.name)

        return tuple(names)

    def conform(self, spec: LoadSpec) -> LoadSpec:
        """Значения потока в форме хранения; неизвестные поля остаются как есть.

        Ошибки:
        CatalogInvariantError — ссылка на колонку не разбирается как UUID.
        """
        by_name = self._fields_by_name()

        values: dict[str, LoadValue] = {}
        for name, value in spec.values.items():
            field = by_name.get(name)
            if field is None:
                values[name] = value
                continue

            try:
                values[name] = field.type.conform(value)
            except ValueError as exc:
                msg = (
                    f"field {name!r} of load kind {self.name!r} "
                    f"is not a column id: {value!r}"
                )
                raise CatalogInvariantError([msg]) from exc

        return LoadSpec(kind_id=spec.kind_id, values=values)

    def violations_of(self, spec: LoadSpec) -> Iterator[str]:
        """Нарушения состава и типов значений относительно полей вида."""
        by_name = self._fields_by_name()

        for name in spec.values:
            if name in by_name:
                continue

            yield f"unknown field {name!r} of load kind {self.name!r}"

        for field in self.fields:
            if field.name not in spec.values:
                if field.required:
                    yield (
                        f"required field {field.name!r} of load kind {self.name!r} "
                        f"is missing"
                    )

                continue

            value = spec.values[field.name]
            if field.type.accepts(value):
                continue

            shape = LoadFieldType.shape_of(value)
            yield (
                f"field {field.name!r} of load kind {self.name!r} "
                f"expects {field.type.value}, got {shape}"
            )

    def column_refs(self, spec: LoadSpec) -> Iterator[tuple[str, UUID]]:
        """Пары (имя поля, id колонки) по всем ссылкам в значениях."""
        by_name = self._fields_by_name()

        for name, value in spec.values.items():
            field = by_name.get(name)
            if field is None:
                continue

            for column_id in field.type.column_ids(value):
                yield name, column_id

    def _fields_by_name(self) -> dict[str, LoadField]:
        by_name: dict[str, LoadField] = {}
        for field in self.fields:
            by_name[field.name] = field

        return by_name


class Layer(CatalogModel):
    """Слой хранения; только имя, порядок — порядок создания."""

    id: UUID
    name: str = Field(min_length=1)


class Dataset(CatalogModel):
    """Набор данных внутри слоя."""

    id: UUID
    layer_id: UUID
    name: str = Field(min_length=1)
    source: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    owner: str = ""


class Column(CatalogModel):
    """Колонка набора; position задаёт порядок в карточке."""

    id: UUID
    dataset_id: UUID
    name: str = Field(min_length=1)
    type: str
    nullable: bool
    is_key: bool
    position: int = Field(ge=0)
    description: str = ""


class LoadSpec(CatalogModel):
    """Правило загрузки потока: вид и значения по его полям."""

    kind_id: UUID
    values: Mapping[str, LoadValue]


class Flow(CatalogModel):
    """Поток данных между двумя наборами с правилом загрузки."""

    id: UUID
    from_dataset_id: UUID
    to_dataset_id: UUID
    load: LoadSpec
    description: str = ""


CatalogEntity = Layer | Dataset | Column | LoadKind | Flow


class EntityKind(StrEnum):
    """Вид сущности каталога; знает поле снимка и вид произвольной сущности."""

    LAYER = "layer"
    DATASET = "dataset"
    COLUMN = "column"
    LOAD_KIND = "load_kind"
    FLOW = "flow"

    @classmethod
    def of(cls, entity: CatalogEntity) -> EntityKind:
        if isinstance(entity, Layer):
            return cls.LAYER

        if isinstance(entity, Dataset):
            return cls.DATASET

        if isinstance(entity, Column):
            return cls.COLUMN

        if isinstance(entity, LoadKind):
            return cls.LOAD_KIND

        return cls.FLOW

    @property
    def table_field(self) -> str:
        """Имя поля CatalogSnapshot с таблицей этого вида."""
        if self is EntityKind.LAYER:
            return "layers"

        if self is EntityKind.DATASET:
            return "datasets"

        if self is EntityKind.COLUMN:
            return "columns"

        if self is EntityKind.LOAD_KIND:
            return "load_kinds"

        return "flows"


class EntityRef(CatalogModel):
    """Ссылка на сущность каталога: вид и id."""

    kind: EntityKind
    id: UUID

    @classmethod
    def of(cls, entity: CatalogEntity) -> EntityRef:
        kind = EntityKind.of(entity)
        return cls(kind=kind, id=entity.id)


class CatalogSnapshot(CatalogModel):
    """Полное состояние каталога одной версии.

    Таблицы сущностей по id. Методы added/replaced/removed возвращают новый
    снимок, не меняя текущий; check() проверяет инварианты целиком.
    """

    layers: Mapping[UUID, Layer]
    datasets: Mapping[UUID, Dataset]
    columns: Mapping[UUID, Column]
    load_kinds: Mapping[UUID, LoadKind]
    flows: Mapping[UUID, Flow]

    @classmethod
    def empty(cls) -> CatalogSnapshot:
        return cls(layers={}, datasets={}, columns={}, load_kinds={}, flows={})

    def table(self, kind: EntityKind) -> Mapping[UUID, CatalogEntity]:
        if kind is EntityKind.LAYER:
            return self.layers

        if kind is EntityKind.DATASET:
            return self.datasets

        if kind is EntityKind.COLUMN:
            return self.columns

        if kind is EntityKind.LOAD_KIND:
            return self.load_kinds

        return self.flows

    def added(self, entity: CatalogEntity) -> CatalogSnapshot:
        """Снимок с новой сущностью.

        Ошибки:
        CatalogInvariantError — id уже занят.
        """
        ref = EntityRef.of(entity)
        table = dict(self.table(ref.kind))
        if entity.id in table:
            msg = f"{self.label(ref)} already exists"
            raise CatalogInvariantError([msg])

        table[entity.id] = entity
        return self._with_table(ref.kind, table)

    def replaced(self, entity: CatalogEntity) -> CatalogSnapshot:
        """Снимок, где сущность с этим id заменена целиком.

        Ошибки:
        CatalogInvariantError — сущности с таким id нет.
        """
        ref = EntityRef.of(entity)
        table = dict(self.table(ref.kind))
        if entity.id not in table:
            msg = f"{self.label(ref)} not found"
            raise CatalogInvariantError([msg])

        table[entity.id] = entity
        return self._with_table(ref.kind, table)

    def removed(self, ref: EntityRef) -> CatalogSnapshot:
        """Снимок без сущности; зависимые не трогаются, их проверяет операция.

        Ошибки:
        CatalogInvariantError — сущности с таким id нет.
        """
        table = dict(self.table(ref.kind))
        if ref.id not in table:
            msg = f"{self.label(ref)} not found"
            raise CatalogInvariantError([msg])

        del table[ref.id]
        return self._with_table(ref.kind, table)

    def conformed(self, flow: Flow) -> Flow:
        """Поток со значениями в форме хранения по его виду; без вида — как есть.

        Ошибки:
        CatalogInvariantError — ссылка на колонку не разбирается как UUID.
        """
        kind = self.load_kinds.get(flow.load.kind_id)
        if kind is None:
            return flow

        load = kind.conform(flow.load)
        return flow.model_copy(update={"load": load})

    def datasets_in(self, layer_id: UUID) -> Iterator[Dataset]:
        for dataset in self.datasets.values():
            if dataset.layer_id != layer_id:
                continue

            yield dataset

    def columns_of(self, dataset_id: UUID) -> Iterator[Column]:
        for column in self.columns.values():
            if column.dataset_id != dataset_id:
                continue

            yield column

    def flows_of(self, dataset_id: UUID) -> Iterator[Flow]:
        """Потоки, входящие в набор или исходящие из него."""
        for flow in self.flows.values():
            if flow.from_dataset_id == dataset_id:
                yield flow
                continue

            if flow.to_dataset_id == dataset_id:
                yield flow

    def flows_of_kind(self, kind_id: UUID) -> Iterator[Flow]:
        for flow in self.flows.values():
            if flow.load.kind_id != kind_id:
                continue

            yield flow

    def flows_using_column(self, column_id: UUID) -> Iterator[Flow]:
        """Потоки, чьи значения загрузки ссылаются на колонку."""
        for flow in self.flows.values():
            kind = self.load_kinds.get(flow.load.kind_id)
            if kind is None:
                continue

            referenced: list[UUID] = []
            for _, referenced_id in kind.column_refs(flow.load):
                referenced.append(referenced_id)

            if column_id not in referenced:
                continue

            yield flow

    def label(self, ref: EntityRef) -> str:
        """Подпись сущности для текстов ошибок: вид и имя, без имени — id."""
        entity = self.table(ref.kind).get(ref.id)
        if entity is None:
            return f"{ref.kind.value} {ref.id}"

        if isinstance(entity, Flow):
            return self._flow_label(entity)

        return f"{ref.kind.value} {entity.name!r}"

    def check(self) -> None:
        """Проверка инвариантов снимка целиком.

        Ошибки:
        CatalogInvariantError — перечень нарушений.
        """
        violations = list(self._violations())
        if not violations:
            return

        raise CatalogInvariantError(violations)

    @staticmethod
    def repeated(keys: Iterable[KeyT]) -> Iterator[KeyT]:
        """Ключи, встреченные больше одного раза, по первому появлению."""
        counts: Counter[KeyT] = Counter()
        for key in keys:
            counts[key] += 1

        for key, count in counts.items():
            if count == 1:
                continue

            yield key

    def _with_table(
        self, kind: EntityKind, table: Mapping[UUID, CatalogEntity]
    ) -> CatalogSnapshot:
        return self.model_copy(update={kind.table_field: dict(table)})

    def _dataset_label(self, dataset_id: UUID) -> str:
        ref = EntityRef(kind=EntityKind.DATASET, id=dataset_id)
        return self.label(ref)

    def _flow_label(self, flow: Flow) -> str:
        source = self._dataset_label(flow.from_dataset_id)
        target = self._dataset_label(flow.to_dataset_id)
        return f"flow {source} -> {target}"

    def _violations(self) -> Iterator[str]:
        yield from self._duplicate_names()
        yield from self._duplicate_positions()
        yield from self._dangling_references()
        yield from self._load_values()

    def _layer_names(self) -> Iterator[str]:
        for layer in self.layers.values():
            yield layer.name

    def _load_kind_names(self) -> Iterator[str]:
        for kind in self.load_kinds.values():
            yield kind.name

    def _dataset_names(self) -> Iterator[tuple[UUID, str]]:
        for dataset in self.datasets.values():
            yield dataset.layer_id, dataset.name

    def _column_names(self) -> Iterator[tuple[UUID, str]]:
        for column in self.columns.values():
            yield column.dataset_id, column.name

    def _column_positions(self) -> Iterator[tuple[UUID, int]]:
        for column in self.columns.values():
            yield column.dataset_id, column.position

    def _duplicate_names(self) -> Iterator[str]:
        for name in self.repeated(self._layer_names()):
            yield f"duplicate layer name {name!r}"

        for layer_id, name in self.repeated(self._dataset_names()):
            layer = self.label(EntityRef(kind=EntityKind.LAYER, id=layer_id))
            yield f"duplicate dataset name {name!r} in {layer}"

        for dataset_id, name in self.repeated(self._column_names()):
            dataset = self._dataset_label(dataset_id)
            yield f"duplicate column name {name!r} in {dataset}"

        for name in self.repeated(self._load_kind_names()):
            yield f"duplicate load kind name {name!r}"

    def _duplicate_positions(self) -> Iterator[str]:
        for dataset_id, position in self.repeated(self._column_positions()):
            dataset = self._dataset_label(dataset_id)
            yield f"duplicate column position {position} in {dataset}"

    def _dangling_references(self) -> Iterator[str]:
        for dataset in self.datasets.values():
            if dataset.layer_id in self.layers:
                continue

            yield f"dataset {dataset.name!r} refers to missing layer {dataset.layer_id}"

        for column in self.columns.values():
            if column.dataset_id in self.datasets:
                continue

            yield (
                f"column {column.name!r} refers to missing dataset {column.dataset_id}"
            )

        for flow in self.flows.values():
            yield from self._flow_references(flow)

    def _flow_references(self, flow: Flow) -> Iterator[str]:
        label = self._flow_label(flow)

        if flow.from_dataset_id not in self.datasets:
            yield f"{label} refers to missing dataset {flow.from_dataset_id}"

        if flow.to_dataset_id not in self.datasets:
            yield f"{label} refers to missing dataset {flow.to_dataset_id}"

        if flow.from_dataset_id == flow.to_dataset_id:
            yield f"{label} loops on the same dataset"

        if flow.load.kind_id not in self.load_kinds:
            yield f"{label} refers to missing load kind {flow.load.kind_id}"

    def _load_values(self) -> Iterator[str]:
        for flow in self.flows.values():
            kind = self.load_kinds.get(flow.load.kind_id)
            if kind is None:
                continue

            label = self._flow_label(flow)
            for text in kind.violations_of(flow.load):
                yield f"{label}: {text}"

            for field_name, column_id in kind.column_refs(flow.load):
                yield from self._column_reference(flow, label, field_name, column_id)

    def _column_reference(
        self, flow: Flow, label: str, field_name: str, column_id: UUID
    ) -> Iterator[str]:
        column = self.columns.get(column_id)
        if column is None:
            yield f"{label}: field {field_name!r} refers to missing column {column_id}"
            return

        if column.dataset_id == flow.from_dataset_id:
            return

        if column.dataset_id == flow.to_dataset_id:
            return

        yield (
            f"{label}: field {field_name!r} refers to column {column.name!r} "
            f"outside the flow datasets"
        )
