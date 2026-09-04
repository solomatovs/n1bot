"""Источники метаданных: адрес объекта, записи, снимок и реестр видов.

Вид источника — это kind типа соединения из реестра boba.connections
(«postgres», «clickhouse»): свой перечень видов каталог не ведёт. Снимок
каждого вида живёт в пакете-владельце драйвера в родной структуре и
регистрируется entry point'ом группы boba.catalog с именем, равным kind
соединения; реестр SourceKinds собирает их на старте. Здесь база, общая для
любого вида: как адресуется объект, как запись знает свой ключ и родителя,
как снимок объявляет части (таблицы записей) и семейства объектов, и что
снимок обязан уметь. Всё, что выводится из объявленной раскладки —
проверка инвариантов, подсчёт объектов, поиск объекта и колонок по адресу,
обход записей по частям, карточка, — живёт в базовом классе; вид источника
добавляет только родное: дерево, колонки узла, правки ручного источника.

Ошибки:
CatalogInvariantError — снимок нарушает инварианты: повторы ключей,
    ссылки на несуществующих родителей.
CatalogError — по адресу нет объекта, вид объекта не из этого источника.
SourceKindsError — entry point снимка не по контракту либо вида нет в
    реестре (пакет-владелец не установлен).
"""

from __future__ import annotations

from abc import abstractmethod
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from enum import StrEnum
from importlib.metadata import entry_points
from operator import attrgetter
from typing import Any, ClassVar, Self, TypeVar
from uuid import UUID

from pydantic import ConfigDict, Field, ValidationError

from boba.catalog.base import (
    CatalogError,
    CatalogInvariantError,
    CatalogModel,
    ChangeStatus,
)

__all__ = [
    "Keyed",
    "NodeColumn",
    "ObjectCard",
    "ObjectFamily",
    "ObjectKind",
    "ObjectRef",
    "PartKind",
    "Records",
    "SnapshotPart",
    "SourceKinds",
    "SourceKindsError",
    "SourceObject",
    "SourceRecord",
    "SourceSnapshot",
    "SubPart",
    "TreeKind",
    "TreeNode",
]


class ObjectKind(StrEnum):
    """Что адресуется в снимке. Postgres: relation, routine, sequence, type;
    ClickHouse: table, dictionary; database и schema — уровни выше объектов."""

    DATABASE = "database"
    SCHEMA = "schema"
    RELATION = "relation"
    ROUTINE = "routine"
    SEQUENCE = "sequence"
    TYPE = "type"
    TABLE = "table"
    DICTIONARY = "dictionary"


class PartKind(StrEnum):
    """Что за часть объекта: колонки, ограничения, индексы, аргументы,
    атрибуты. Ими различаются изменения в diff и подчасти семейства."""

    COLUMN = "column"
    CONSTRAINT = "constraint"
    INDEX = "index"
    ARGUMENT = "argument"
    ATTRIBUTE = "attribute"


class ObjectRef(CatalogModel):
    """Адрес объекта источника, стабильный между версиями: вид объекта и
    родной путь. Postgres: (database, schema, name), рутина — плюс сигнатура;
    ClickHouse: (database, name)."""

    source_id: UUID
    kind: ObjectKind
    path: tuple[str, ...] = Field(min_length=1)

    def render(self) -> str:
        return "/".join(self.path)


class SourceRecord(CatalogModel):
    """Строка снимка: одна запись одной части. Ключ — значения полей KEY,
    родитель — значения полей PARENT; подкласс объявляет оба списка, а
    хранилище по ним же строит уникальность и связь таблиц."""

    KEY: ClassVar[tuple[str, ...]] = ()
    PARENT: ClassVar[tuple[str, ...]] = ()
    ORDER: ClassVar[tuple[str, ...]] = ()
    """Поля, по которым записи одного родителя идут по порядку (ordinal,
    position); пусто — порядок ключа."""
    COLUMN_NAMES: ClassVar[Mapping[str, str]] = {}
    """Поля, чьё имя в таблице хранения другое (schema_name → schema)."""
    VOLATILE: ClassVar[frozenset[str]] = frozenset()
    """Статистика, которая меняется без изменения структуры (число строк,
    размер, последнее значение последовательности): хранится в версии, но
    разницей версий не считается."""

    def structural(self) -> Mapping[str, Any]:
        """Поля записи без летучей статистики: то, что сравнивает diff."""
        dumped: dict[str, Any] = self.model_dump(mode="json")
        for field in self.VOLATILE:
            dumped.pop(field, None)

        return dumped

    @property
    def key(self) -> tuple[str, ...]:
        return tuple(str(getattr(self, field)) for field in self.KEY)

    @property
    def parent(self) -> tuple[str, ...]:
        return tuple(str(getattr(self, field)) for field in self.PARENT)

    @property
    def label(self) -> str:
        return self.key[-1]

    @property
    def order(self) -> tuple[object, ...]:
        """Ключ сортировки среди записей одного родителя."""
        if not self.ORDER:
            return self.key

        return tuple(getattr(self, field) for field in self.ORDER)


class SourceObject(SourceRecord):
    """Запись с адресом: объект процесса (relation, routine, sequence, type,
    table, dictionary). Database и schema адресуются тоже, но объектами
    процесса не становятся. Свою карточку объект собирает сам из снимка."""

    @property
    @abstractmethod
    def object_kind(self) -> ObjectKind: ...

    @abstractmethod
    def card(self, snapshot: SourceSnapshot, ref: ObjectRef) -> ObjectCard:
        """Родная карточка объекта с его частями из снимка."""


class NodeColumn(CatalogModel):
    """Колонка объекта глазами процесса: имя, тип, nullable и вхождение в
    первичный ключ. Общая форма для всех видов источников, чтобы карточка
    узла рисовалась одинаково."""

    name: str
    type: str
    nullable: bool
    key: bool


class ObjectCard(CatalogModel):
    """База карточек объектов для панели деталей: подкласс держит запись и
    её части в родной форме и различается литералом card, который объявляет
    пакет-владелец вида."""

    ref: ObjectRef


class TreeKind(StrEnum):
    """Что за узел в дереве источника."""

    DATABASE = "database"
    SCHEMA = "schema"
    GROUP = "group"
    OBJECT = "object"


class TreeNode(CatalogModel):
    """Узел дерева источника любой глубины. path — путь узла в дереве (не
    адрес объекта: у групп своя ступень), ref — адрес, если узел — объект."""

    path: tuple[str, ...] = Field(min_length=1)
    label: str
    kind: TreeKind
    children_count: int = Field(ge=0)
    detail: str = ""
    comment: str | None = None
    ref: ObjectRef | None = None
    status: ChangeStatus = ChangeStatus.UNCHANGED


class SnapshotPart(CatalogModel):
    """Часть снимка — одна таблица записей: имя поля снимка, модель записи,
    подпись для сообщений, часть-родитель (пусто у корня)."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    name: str
    model: type[SourceRecord]
    label: str
    parent: str | None = None


class SubPart(CatalogModel):
    """Подчасть семейства объектов: какая часть снимка хранит его колонки,
    ограничения, индексы, аргументы или атрибуты."""

    kind: PartKind
    part: str


class ObjectFamily(CatalogModel):
    """Семейство объектов одного вида: часть с самими объектами и её
    подчасти. По семействам считаются объекты, строится diff, ищутся объект
    и колонки по адресу."""

    kind: ObjectKind
    part: str
    subparts: tuple[SubPart, ...] = ()

    def subpart(self, kind: PartKind) -> str | None:
        for item in self.subparts:
            if item.kind is kind:
                return item.part

        return None


RecordT = TypeVar("RecordT", bound=SourceRecord)


class Records:
    """Выборки из кортежа записей: без записи по ключу, без детей родителя,
    только записи нужной модели."""

    @staticmethod
    def of_type(
        records: Iterable[SourceRecord], model: type[RecordT]
    ) -> tuple[RecordT, ...]:
        kept: list[RecordT] = []
        for record in records:
            if isinstance(record, model):
                kept.append(record)

        return tuple(kept)

    @staticmethod
    def without_key(
        records: Sequence[RecordT], key: tuple[str, ...]
    ) -> tuple[RecordT, ...]:
        kept: list[RecordT] = []
        for record in records:
            if record.key == key:
                continue

            kept.append(record)

        return tuple(kept)

    @staticmethod
    def without_parent(
        records: Sequence[RecordT], parent: tuple[str, ...]
    ) -> tuple[RecordT, ...]:
        kept: list[RecordT] = []
        for record in records:
            if record.parent == parent:
                continue

            kept.append(record)

        return tuple(kept)


class Keyed:
    """Проверки таблиц снимка: ключи уникальны, у каждой записи есть родитель."""

    @staticmethod
    def keys_of(records: Iterable[SourceRecord]) -> set[tuple[str, ...]]:
        keys: set[tuple[str, ...]] = set()
        for record in records:
            keys.add(record.key)

        return keys

    @staticmethod
    def duplicates(records: Iterable[SourceRecord]) -> Iterator[tuple[str, ...]]:
        keys: list[tuple[str, ...]] = []
        for record in records:
            keys.append(record.key)

        counts = Counter(keys)
        for key, count in counts.items():
            if count == 1:
                continue

            yield key

    @staticmethod
    def require_unique(label: str, records: Iterable[SourceRecord]) -> None:
        violations: list[str] = []
        for key in Keyed.duplicates(records):
            violations.append(f"duplicate {label} {'/'.join(key)}")

        if violations:
            raise CatalogInvariantError(violations)

    @staticmethod
    def require_parents(
        label: str, records: Iterable[SourceRecord], parents: set[tuple[str, ...]]
    ) -> None:
        violations: list[str] = []
        for record in records:
            if record.parent in parents:
                continue

            key = "/".join(record.key)
            parent = "/".join(record.parent)
            violations.append(f"{label} {key} has no parent {parent}")

        if violations:
            raise CatalogInvariantError(violations)


class SourceSnapshot(CatalogModel):
    """Снимок источника одной версии: плоские таблицы записей по частям.

    Подкласс объявляет вид (литерал kind), префикс таблиц хранения, части
    (PARTS, в порядке от родителей к детям) и семейства объектов
    (FAMILIES). По этим объявлениям база проверяет инварианты, считает
    объекты, находит объект и колонки по адресу, отдаёт записи по частям.
    Родное у подкласса: дерево (children), карточка объекта (card) и колонки
    узла (node_columns). Каждый подкласс регистрируется в SourceKinds по
    своему виду при объявлении.
    """

    TABLE_PREFIX: ClassVar[str] = ""
    PARTS: ClassVar[tuple[SnapshotPart, ...]] = ()
    FAMILIES: ClassVar[tuple[ObjectFamily, ...]] = ()
    SYNC_TOOL: ClassVar[str] = ""
    """Имя инструмента, который снимает структуру источника кадрами
    синхронизации; пусто — у вида нет синхронизации."""

    kind: str
    """kind типа соединения; подкласс закрепляет его литералом."""

    @classmethod
    def source_kind(cls) -> str:
        """kind вида, закреплённый подклассом литералом по умолчанию.

        Ошибки:
        SourceKindsError — у класса kind не закреплён.
        """
        field = cls.model_fields["kind"]
        default = field.default
        if not isinstance(default, str) or default == "":
            msg = (
                f"{cls.__name__} does not pin its kind: expected a Literal default "
                f"of the field kind, got {default!r}"
            )
            raise SourceKindsError(msg)

        return default

    @classmethod
    def empty(cls) -> Self:
        # у подкласса kind задан литералом по умолчанию, у базы его нет
        return cls.model_validate({})

    @classmethod
    def parts(cls) -> tuple[SnapshotPart, ...]:
        return cls.PARTS

    @classmethod
    def sync_tool(cls) -> str:
        """Ошибки:
        CatalogError — у вида нет инструмента снятия.
        """
        if cls.SYNC_TOOL:
            return cls.SYNC_TOOL

        msg = (
            f"{cls.source_kind()} sources declare no sync tool: "
            "the snapshot class has an empty SYNC_TOOL"
        )
        raise CatalogError(msg)

    @classmethod
    def part(cls, name: str) -> SnapshotPart:
        """Ошибки:
        CatalogError — части с таким именем у снимка нет.
        """
        for part in cls.PARTS:
            if part.name == name:
                return part

        known = [part.name for part in cls.PARTS]
        msg = f"{cls.__name__} has no part {name!r}, its parts: {known}"
        raise CatalogError(msg)

    @classmethod
    def families(cls) -> tuple[ObjectFamily, ...]:
        return cls.FAMILIES

    @classmethod
    def family(cls, kind: ObjectKind) -> ObjectFamily:
        """Ошибки:
        CatalogError — объекты такого вида в этом источнике не адресуются.
        """
        for family in cls.FAMILIES:
            if family.kind is kind:
                return family

        msg = (
            f"{kind.value} is not an object kind of a {cls.source_kind()} source, "
            f"its kinds: {[family.kind.value for family in cls.FAMILIES]}"
        )
        raise CatalogError(msg)

    def records_of(self, part: str) -> tuple[SourceRecord, ...]:
        """Записи части по имени поля снимка."""
        self.part(part)
        return tuple(getattr(self, part))

    def with_records(self, part: str, records: Iterable[SourceRecord]) -> Self:
        """Копия снимка с заменённой частью."""
        self.part(part)
        return self.model_copy(update={part: tuple(records)})

    def check(self) -> None:
        """Ключи уникальны в каждой части, у каждой записи есть родитель в
        части-родителе.

        Ошибки:
        CatalogInvariantError — с перечнем нарушений.
        """
        for part in self.PARTS:
            Keyed.require_unique(part.label, self.records_of(part.name))

        for part in self.PARTS:
            if part.parent is None:
                continue

            parents = Keyed.keys_of(self.records_of(part.parent))
            Keyed.require_parents(part.label, self.records_of(part.name), parents)

    def objects_count(self) -> int:
        count = 0
        for family in self.FAMILIES:
            count += len(self.records_of(family.part))

        return count

    def object_at(self, ref: ObjectRef) -> SourceObject | None:
        """Объект по адресу; None — в снимке его нет.

        Ошибки:
        CatalogError — вид объекта не из этого источника.
        """
        family = self.family(ref.kind)
        for record in self.records_of(family.part):
            if record.key != ref.path:
                continue

            if isinstance(record, SourceObject):
                return record

        return None

    def require_object(self, ref: ObjectRef) -> SourceObject:
        """Ошибки:
        CatalogError — по адресу нет объекта или вид не из этого источника.
        """
        found = self.object_at(ref)
        if found is None:
            msg = f"no {ref.kind.value} at {ref.render()} in the {self.kind} snapshot"
            raise CatalogError(msg)

        return found

    def exists(self, ref: ObjectRef) -> bool:
        try:
            return self.object_at(ref) is not None
        except CatalogError:
            return False

    def parts_of(self, ref: ObjectRef, kind: PartKind) -> tuple[SourceRecord, ...]:
        """Записи подчасти объекта (колонки, ограничения, …) в порядке снимка;
        пусто, если у семейства нет такой подчасти."""
        part = self.family(ref.kind).subpart(kind)
        if part is None:
            return ()

        matched: list[SourceRecord] = []
        for record in self.records_of(part):
            if record.parent == ref.path:
                matched.append(record)

        matched.sort(key=attrgetter("order"))
        return tuple(matched)

    def parts_of_type(
        self, ref: ObjectRef, kind: PartKind, model: type[RecordT]
    ) -> tuple[RecordT, ...]:
        """Подчасть объекта записями своей модели, для сборки карточек."""
        return Records.of_type(self.parts_of(ref, kind), model)

    def card(self, ref: ObjectRef) -> ObjectCard:
        """Родная карточка объекта с его частями.

        Ошибки:
        CatalogError — по адресу нет объекта или вид не из этого источника.
        """
        return self.require_object(ref).card(self, ref)

    def column_names(self, ref: ObjectRef) -> Sequence[str] | None:
        """Имена колонок объекта; None — объекта нет или колонок у его вида
        не бывает."""
        if not self.exists(ref):
            return None

        if self.family(ref.kind).subpart(PartKind.COLUMN) is None:
            return None

        names: list[str] = []
        for column in self.parts_of(ref, PartKind.COLUMN):
            names.append(column.label)

        return names

    @abstractmethod
    def children(self, source_id: UUID, path: Sequence[str]) -> Sequence[TreeNode]:
        """Дети узла дерева по пути; корень — пустой путь."""

    @abstractmethod
    def node_columns(self, ref: ObjectRef) -> tuple[NodeColumn, ...]:
        """Колонки объекта для карточки узла; у объектов без колонок пусто."""


class SourceKindsError(CatalogError):
    """Реестр видов: entry point не по контракту или вида нет в реестре."""


class SourceKinds:
    """Реестр видов источников: kind типа соединения → класс снимка.

    Собирается один раз на старте из entry points группы boba.catalog, где
    имя entry point — kind соединения, а значение — класс снимка; в тестах
    собирается из классов напрямую (of). Дальше по нему находят класс,
    пустой снимок и разбирают снимок из JSON по полю kind.
    """

    GROUP: ClassVar[str] = "boba.catalog"

    def __init__(self, table: Mapping[str, type[SourceSnapshot]]) -> None:
        self._table = dict(table)

    @classmethod
    def of(cls, *snapshots: type[SourceSnapshot]) -> SourceKinds:
        """Реестр из классов: kind берётся из литерала класса."""
        table: dict[str, type[SourceSnapshot]] = {}
        for snapshot in snapshots:
            table[snapshot.source_kind()] = snapshot

        return cls(table)

    @classmethod
    def discover(cls) -> SourceKinds:
        """Реестр из entry points установленных пакетов.

        Ошибки:
        SourceKindsError — значение entry point не класс снимка либо его kind
            не равен имени entry point.
        """
        table: dict[str, type[SourceSnapshot]] = {}
        for entry in entry_points(group=cls.GROUP):
            loaded = entry.load()
            if not isinstance(loaded, type) or not issubclass(loaded, SourceSnapshot):
                msg = (
                    f"entry point {entry.name!r} of group {cls.GROUP!r} "
                    f"({entry.value}): expected a SourceSnapshot subclass, "
                    f"got {loaded!r}"
                )
                raise SourceKindsError(msg)

            if loaded.source_kind() != entry.name:
                msg = (
                    f"entry point {entry.name!r} of group {cls.GROUP!r} "
                    f"({entry.value}) pins kind {loaded.source_kind()!r}: "
                    "the entry point name must equal the kind"
                )
                raise SourceKindsError(msg)

            table[entry.name] = loaded

        return cls(table)

    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._table))

    def known(self, kind: str) -> bool:
        return kind in self._table

    def snapshot_class(self, kind: str) -> type[SourceSnapshot]:
        """Ошибки:
        SourceKindsError — вида нет в реестре.
        """
        found = self._table.get(kind)
        if found is None:
            msg = (
                f"source kind {kind!r} has no snapshot class, installed kinds: "
                f"{list(self.kinds())}"
            )
            raise SourceKindsError(msg)

        return found

    def empty(self, kind: str) -> SourceSnapshot:
        return self.snapshot_class(kind).empty()

    def registered(self) -> tuple[type[SourceSnapshot], ...]:
        return tuple(self._table[kind] for kind in self.kinds())

    def parse(self, raw: Mapping[str, Any]) -> SourceSnapshot:
        """Снимок из JSON: класс выбирается по полю kind.

        Ошибки:
        SourceKindsError — kind отсутствует, неизвестен или снимок не по модели.
        """
        kind = raw.get("kind")
        if not isinstance(kind, str):
            msg = (
                "source snapshot: expected a string field kind, "
                f"got kind={kind!r} among keys {sorted(raw)}"
            )
            raise SourceKindsError(msg)

        try:
            return self.snapshot_class(kind).model_validate(raw)
        except ValidationError as exc:
            msg = f"source snapshot of kind {kind!r} does not match its model: {exc}"
            raise SourceKindsError(msg) from exc
