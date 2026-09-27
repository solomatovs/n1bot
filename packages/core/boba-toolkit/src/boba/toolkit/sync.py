"""Ядро приёмника с управлением схемой: нейтральное описание колонок,
сверка схемы потока со схемой таблицы-приёмника и стратегии — что делать
с таблицей, что удалить, как вставить. Ничего не знает ни о pyarrow, ни о
драйверах: поле потока сюда переводит boba.toolkit.arrow, колонку каталога
— модуль движка, SQL по стратегиям строит реализация порта TableDdl в
инфра-пакете движка. Стратегии — модели с kind: LLM передаёт их объектом,
поведение живёт в модели.

Ошибки:
SyncError — правило вызова не сходится со схемами (rename на поле,
    которого нет в потоке) или стратегия отказала (FAIL с причиной).
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.ports import Inbound, Outbound
from boba.toolkit.stream import Chunk

__all__ = [
    "ArrowFieldMeta",
    "BackupAndCreate",
    "BackupAndCreateIfSchemaChanged",
    "ColumnDeclaration",
    "ColumnMatch",
    "ColumnRule",
    "ColumnRules",
    "ColumnSpec",
    "ColumnType",
    "ColumnTypeModel",
    "ColumnVerdict",
    "ContractText",
    "CreateIfNotExists",
    "Declarations",
    "DeclaredType",
    "DeleteAll",
    "DeleteNothing",
    "DeleteOutcome",
    "DeleteStrategy",
    "DeleteStrategyApply",
    "DeleteWhere",
    "DiffSummary",
    "DoNothing",
    "DropAndCreate",
    "DropAndCreateIfSchemaChanged",
    "Engine",
    "ErrorIfNotExists",
    "ErrorIfSchemaChanged",
    "FailOnUnknown",
    "FallbackAsVarchar",
    "FrameBodies",
    "FrameWriter",
    "InsertFull",
    "InsertNothing",
    "InsertSink",
    "InsertStrategy",
    "InsertStrategyApply",
    "RowsFrame",
    "SchemaAction",
    "SchemaDiff",
    "SchemaFrame",
    "SchemaMatcher",
    "SchemaPlan",
    "SchemaStrategy",
    "SchemaStrategyPlan",
    "SourceModes",
    "StreamColumn",
    "StreamContract",
    "SyncError",
    "SyncFrame",
    "SyncInbound",
    "SyncOutbound",
    "SyncReport",
    "TableColumn",
    "TableDdl",
    "TableSpec",
    "TimeUnit",
    "TypeComparer",
    "TypeFamily",
    "UnknownTypeApply",
    "UnknownTypeStrategy",
    "Verdict",
    "WireFormat",
]


class SyncError(Exception):
    """Правило вызова не сходится со схемами или стратегия отказала."""


class ArrowFieldMeta(StrEnum):
    """Ключи metadata поля Arrow, которыми источник дополняет схему потока:
    движок источника, текст типа в его диалекте и длина строки."""

    SOURCE_ENGINE = "source_engine"
    SOURCE_TYPE = "source_type"
    CHAR_LENGTH = "char_length"


class TypeFamily(StrEnum):
    """Семейство типа колонки: сравнимы только колонки одного семейства."""

    INTEGER = "integer"
    FLOAT = "float"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    STRING = "string"
    BINARY = "binary"
    DATE = "date"
    TIMESTAMP = "timestamp"
    TIME = "time"
    UUID = "uuid"
    JSON = "json"
    INTERVAL = "interval"
    NETWORK = "network"
    BIT = "bit"
    MONEY = "money"
    XML = "xml"
    GEOMETRY = "geometry"
    RANGE = "range"
    TEXTSEARCH = "textsearch"
    ARRAY = "array"
    SYSTEM = "system"
    OTHER = "other"

    def named_only(self) -> bool:
        """Семейство без нейтральных параметров: сравнимо только по имени
        типа одного движка, между движками сверить нельзя."""
        return self in (
            TypeFamily.GEOMETRY,
            TypeFamily.RANGE,
            TypeFamily.TEXTSEARCH,
            TypeFamily.ARRAY,
            TypeFamily.SYSTEM,
            TypeFamily.OTHER,
        )


class TimeUnit(StrEnum):
    """Единица времени timestamp и time; порядок — от грубой к точной."""

    SECOND = "s"
    MILLISECOND = "ms"
    MICROSECOND = "us"
    NANOSECOND = "ns"

    def rank(self) -> int:
        return list(TimeUnit).index(self)


@dataclass(frozen=True)
class ColumnType:
    """Тип колонки в нейтральном виде: семейство и то, что в нём сравнимо —
    ширина целых и float в битах, precision/scale у decimal, единица
    времени и наличие пояса у timestamp и time. text — как тип печатает
    источник, только для сообщений."""

    family: TypeFamily
    text: str
    bits: int = 0
    unsigned: bool = False
    precision: int = 0
    scale: int = 0
    unit: TimeUnit = TimeUnit.MICROSECOND
    zoned: bool = False


@dataclass(frozen=True)
class ColumnSpec:
    """Колонка потока или таблицы: имя, тип, nullable, позиция, а также то,
    что известно о ней в диалекте источника — текст типа и длина строки
    (0 — неизвестна)."""

    name: str
    kind: ColumnType
    nullable: bool
    position: int
    source_type: str = ""
    char_length: int = 0

    def describe(self) -> str:
        parts = [self.kind.text]
        if self.source_type:
            parts.append(f"[{self.source_type}]")

        if not self.nullable:
            parts.append("not null")

        return " ".join(parts)


@dataclass(frozen=True)
class DeclaredType:
    """Тип из rules.column_types, разобранный движком приёмника: семейство и
    параметры для сверки, текст типа и длина строки как их печатает
    приёмник. Подменяет тип поля потока в сверке и в DDL."""

    kind: ColumnType
    source_type: str
    char_length: int


@dataclass(frozen=True)
class ColumnRule:
    """Правило вызова для колонки приёмника name: rename_from — имя поля
    потока, которое ложится в неё; ddl_type — тип колонки для create table
    текстом как есть (пусто — приёмник выводит сам)."""

    name: str
    rename_from: str = ""
    ddl_type: str = ""


class ColumnRules(BaseModel):
    """Правила приёмника по колонкам: rename_columns — откуда берётся
    колонка, column_types — какой тип ей дать в DDL. Контракт потока
    приёмник не правит."""

    model_config = ConfigDict(frozen=True)

    rename_columns: Mapping[str, str] = Field(
        default={},
        description=(
            "Колонка приёмника -> поле потока, которое в неё ложится: "
            '{"created_at": "created"}. Только имя, данные не меняются.'
        ),
    )
    column_types: Mapping[str, str] = Field(
        default={},
        description=(
            "Колонка приёмника -> тип для create table текстом как есть: "
            '{"v": "vector(3)", "amount": "numeric(20,6)", "m": "sales.mood"}. '
            "Перекрывает и стратегию unknown_types, и вывод типа из потока."
        ),
    )


class Verdict(StrEnum):
    """Итог сверки колонки: ошибка не даёт загрузить без пересоздания."""

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class ColumnVerdict:
    level: Verdict
    message: str


class TypeComparer:
    """Правила совместимости типа потока (source) с типом колонки приёмника
    (target):
        - семейство типов совпадает
        - целые и float — ширина приёмника не меньше
        - decimal — precision и scale приёмника не меньше
            шире — предупреждение
        - строки — длина приёмника не меньше длины источника,
            когда обе известны
        - timestamp и time — единица приёмника не грубее, пояс тот же
        - bit — длина приёмника не меньше, как у строк
        - семейства только по имени (array, range, geometry, textsearch,
            system, other) — при exact оба типа названы и совпадают; без
            имени или между движками — предупреждение, что сверить нельзя
        - nullable источника при not null приёмника — ошибка,
            обратное — предупреждение
    exact — источник и приёмник один движок: сверх семейства сравнивается
    текст типа, расхождение при совместимых семействах — предупреждение.
    """

    def __init__(self, exact: bool) -> None:
        self._exact = exact

    def compare(self, source: ColumnSpec, target: ColumnSpec) -> ColumnVerdict:
        kinds = self._kinds(source, target)
        if kinds.level is Verdict.ERROR:
            return kinds

        nullable = self._nullable(source, target)
        if nullable.level is Verdict.ERROR:
            return nullable

        if kinds.level is Verdict.WARNING:
            return kinds

        if nullable.level is Verdict.WARNING:
            return nullable

        return self._texts(source, target)

    def _kinds(self, source: ColumnSpec, target: ColumnSpec) -> ColumnVerdict:
        src = source.kind
        tgt = target.kind
        if src.family is not tgt.family:
            return ColumnVerdict(
                Verdict.ERROR,
                f"type family differs: stream {src.text}, table {tgt.text}",
            )

        if src.family.named_only():
            return self._named(source, target)

        rules: Mapping[TypeFamily, Callable[[ColumnSpec, ColumnSpec], ColumnVerdict]]
        rules = {
            TypeFamily.INTEGER: self._width,
            TypeFamily.FLOAT: self._width,
            TypeFamily.DECIMAL: self._decimal,
            TypeFamily.STRING: self._length,
            TypeFamily.BIT: self._length,
            TypeFamily.TIMESTAMP: self._moment,
            TypeFamily.TIME: self._moment,
        }
        rule = rules.get(src.family)
        if rule is None:
            return ColumnVerdict(Verdict.OK, "ok")

        return rule(source, target)

    def _width(self, source: ColumnSpec, target: ColumnSpec) -> ColumnVerdict:
        """Сравнивается величина, а не ширина: у знакового на бит меньше —
        uint32 в int32 не влезает, int32 в uint32 теряет знак."""
        src = source.kind
        tgt = target.kind
        if src.unsigned and not tgt.unsigned and src.bits >= tgt.bits:
            return ColumnVerdict(
                Verdict.ERROR,
                f"table {tgt.text} is narrower than stream {src.text}",
            )

        if not src.unsigned and tgt.unsigned:
            return ColumnVerdict(
                Verdict.ERROR,
                f"table {tgt.text} has no sign, stream {src.text} is signed",
            )

        if src.bits > tgt.bits:
            return ColumnVerdict(
                Verdict.ERROR,
                f"table {tgt.text} is narrower than stream {src.text}",
            )

        if src.bits < tgt.bits:
            return ColumnVerdict(
                Verdict.WARNING,
                f"table {tgt.text} is wider than stream {src.text}",
            )

        return ColumnVerdict(Verdict.OK, "ok")

    def _decimal(self, source: ColumnSpec, target: ColumnSpec) -> ColumnVerdict:
        src = source.kind
        tgt = target.kind
        if src.precision == 0:
            if tgt.precision == 0:
                return ColumnVerdict(Verdict.OK, "ok")

            return ColumnVerdict(
                Verdict.WARNING,
                f"stream {src.text} has no declared precision, table {tgt.text} "
                f"may truncate",
            )

        if tgt.precision == 0:
            return ColumnVerdict(Verdict.OK, "ok")

        return self._decimal_digits(src, tgt)

    def _decimal_digits(self, src: ColumnType, tgt: ColumnType) -> ColumnVerdict:
        """Обе точности объявлены: scale и целые разряды приёмника не меньше."""
        if src.scale > tgt.scale:
            return ColumnVerdict(
                Verdict.ERROR,
                f"table {tgt.text} truncates the scale of stream {src.text}",
            )

        integer_src = src.precision - src.scale
        integer_tgt = tgt.precision - tgt.scale
        if integer_src > integer_tgt:
            return ColumnVerdict(
                Verdict.ERROR,
                f"table {tgt.text} holds fewer integer digits than stream {src.text}",
            )

        if src.scale < tgt.scale or integer_src < integer_tgt:
            return ColumnVerdict(
                Verdict.WARNING,
                f"table {tgt.text} is wider than stream {src.text}",
            )

        return ColumnVerdict(Verdict.OK, "ok")

    def _length(self, source: ColumnSpec, target: ColumnSpec) -> ColumnVerdict:
        if source.char_length == 0:
            if target.char_length == 0:
                return ColumnVerdict(Verdict.OK, "ok")

            return ColumnVerdict(
                Verdict.WARNING,
                f"table limits the length to {target.char_length}, the stream "
                f"length is unknown",
            )

        if target.char_length == 0:
            return ColumnVerdict(Verdict.OK, "ok")

        if source.char_length > target.char_length:
            return ColumnVerdict(
                Verdict.ERROR,
                f"table length {target.char_length} is shorter than stream "
                f"length {source.char_length}",
            )

        if source.char_length < target.char_length:
            return ColumnVerdict(
                Verdict.WARNING,
                f"table length {target.char_length} is longer than stream "
                f"length {source.char_length}",
            )

        return ColumnVerdict(Verdict.OK, "ok")

    def _moment(self, source: ColumnSpec, target: ColumnSpec) -> ColumnVerdict:
        src = source.kind
        tgt = target.kind
        if src.zoned != tgt.zoned:
            return ColumnVerdict(
                Verdict.ERROR,
                f"time zone differs: stream {src.text}, table {tgt.text}",
            )

        if src.unit.rank() > tgt.unit.rank():
            return ColumnVerdict(
                Verdict.ERROR,
                f"table {tgt.text} is coarser than stream {src.text}",
            )

        if src.unit.rank() < tgt.unit.rank():
            return ColumnVerdict(
                Verdict.WARNING,
                f"table {tgt.text} is finer than stream {src.text}",
            )

        return ColumnVerdict(Verdict.OK, "ok")

    def _named(self, source: ColumnSpec, target: ColumnSpec) -> ColumnVerdict:
        """Семейство без параметров сравнимо только по имени типа одного
        движка."""
        src = source.source_type
        tgt = target.source_type
        if not src:
            src = source.kind.text

        if not tgt:
            tgt = target.kind.text

        named = bool(source.source_type) and bool(target.source_type)
        if not named:
            return ColumnVerdict(
                Verdict.WARNING,
                f"type cannot be verified, the source named no type: stream {src}, "
                f"table {tgt}",
            )

        if not self._exact:
            return ColumnVerdict(
                Verdict.WARNING,
                f"type cannot be verified across engines: stream {src}, table {tgt}",
            )

        if src != tgt:
            return ColumnVerdict(
                Verdict.ERROR, f"type differs: stream {src}, table {tgt}"
            )

        return ColumnVerdict(Verdict.OK, "ok")

    def _texts(self, source: ColumnSpec, target: ColumnSpec) -> ColumnVerdict:
        if not self._exact:
            return ColumnVerdict(Verdict.OK, "ok")

        if not source.source_type:
            return ColumnVerdict(Verdict.OK, "ok")

        if not target.source_type:
            return ColumnVerdict(Verdict.OK, "ok")

        if source.source_type == target.source_type:
            return ColumnVerdict(Verdict.OK, "ok")

        return ColumnVerdict(
            Verdict.WARNING,
            f"type differs: stream {source.source_type}, table {target.source_type}",
        )

    def _nullable(self, source: ColumnSpec, target: ColumnSpec) -> ColumnVerdict:
        if source.nullable and not target.nullable:
            return ColumnVerdict(
                Verdict.ERROR, "stream is nullable, table column is not null"
            )

        if not source.nullable and target.nullable:
            return ColumnVerdict(
                Verdict.WARNING, "stream is not null, table column is nullable"
            )

        return ColumnVerdict(Verdict.OK, "ok")


@dataclass(frozen=True)
class ColumnMatch:
    """Колонка приёмника name с тем, что о ней известно с трёх сторон:
    поле потока (после rename), колонка таблицы и правило вызова. Любая
    из сторон может отсутствовать — в этом и состоит сверка."""

    name: str
    source: ColumnSpec | None
    target: ColumnSpec | None
    rule: ColumnRule | None

    def verdict(self, comparer: TypeComparer) -> ColumnVerdict:
        if self.source is None and self.target is None:
            return ColumnVerdict(
                Verdict.ERROR,
                f"column {self.name}: neither in the stream nor in the table, "
                f"only in the call rules",
            )

        if self.source is None:
            return ColumnVerdict(
                Verdict.ERROR,
                f"column {self.name}: in the table but not in the stream",
            )

        if self.target is None:
            return ColumnVerdict(
                Verdict.ERROR,
                f"column {self.name}: in the stream but not in the table",
            )

        compared = comparer.compare(self.source, self.target)
        if compared.level is not Verdict.ERROR:
            return compared

        return ColumnVerdict(
            Verdict.ERROR,
            f"column {self.name}: {compared.message} "
            f"(stream {self.source.describe()}, table {self.target.describe()})",
        )


@dataclass(frozen=True)
class TableColumn:
    """Колонка для DDL и загрузки: имя в таблице, имя поля потока, тип,
    nullable, текст типа и длина у источника, явный ddl_type из правил
    приёмника."""

    name: str
    source_name: str
    kind: ColumnType
    nullable: bool
    source_type: str = ""
    char_length: int = 0
    ddl_type: str = ""


@dataclass(frozen=True)
class TableSpec:
    """Колонки таблицы-приёмника в порядке полей потока."""

    columns: Sequence[TableColumn]

    def names(self) -> list[str]:
        return [column.name for column in self.columns]

    def source_names(self) -> list[str]:
        return [column.source_name for column in self.columns]


@dataclass(frozen=True)
class DiffSummary:
    """Итог сверки для стратегии: есть ли ошибки и текст причин."""

    changed: bool
    reason: str


class SchemaDiff:
    """Результат сверки: вердикт по каждой колонке, ошибки и предупреждения
    списком, текст для чата и колонки для DDL."""

    def __init__(self, matches: Sequence[ColumnMatch], comparer: TypeComparer) -> None:
        self._matches = tuple(matches)
        self._verdicts: list[tuple[ColumnMatch, ColumnVerdict]] = []
        for match in self._matches:
            self._verdicts.append((match, match.verdict(comparer)))

    @property
    def matches(self) -> Sequence[ColumnMatch]:
        return self._matches

    def errors(self) -> list[str]:
        return list(self._messages(Verdict.ERROR))

    def warnings(self) -> list[str]:
        return list(self._messages(Verdict.WARNING))

    def summary(self) -> DiffSummary:
        errors = self.errors()
        if not errors:
            return DiffSummary(changed=False, reason="")

        return DiffSummary(changed=True, reason="\n".join(errors))

    def render(self) -> str:
        lines: list[str] = []
        for match, verdict in self._verdicts:
            lines.append(f"- {verdict.level.value} {match.name}: {verdict.message}")

        return "\n".join(lines)

    def table_spec(self) -> TableSpec:
        columns: list[TableColumn] = []
        for match in self._matches:
            if match.source is None:
                continue

            columns.append(
                TableColumn(
                    name=match.name,
                    source_name=match.source.name,
                    kind=match.source.kind,
                    nullable=match.source.nullable,
                    source_type=match.source.source_type,
                    char_length=match.source.char_length,
                    ddl_type=self._ddl_type(match),
                )
            )

        return TableSpec(columns=tuple(columns))

    def _ddl_type(self, match: ColumnMatch) -> str:
        if match.rule is None:
            return ""

        return match.rule.ddl_type

    def _messages(self, level: Verdict) -> Iterator[str]:
        for _, verdict in self._verdicts:
            if verdict.level is level:
                yield verdict.message


class SchemaMatcher:
    """Объединение полей потока, колонок таблицы и правил приёмника по имени
    колонки: поле потока rename_from получает имя name, дальше стороны
    сводятся в ColumnMatch в порядке полей потока, затем колонки, которых в
    потоке нет; ddl_type правила уходит в TableColumn. exact — источник и
    приёмник один движок, типы сверяются и по тексту."""

    def __init__(self, rules: ColumnRules, exact: bool) -> None:
        self._rules = self._rules_of(rules)
        self._comparer = TypeComparer(exact)

    def diff(
        self,
        stream: Sequence[ColumnSpec],
        table: Sequence[ColumnSpec],
        declared: Mapping[str, DeclaredType],
    ) -> SchemaDiff:
        """declared — типы из rules.column_types, разобранные приёмником, по
        имени колонки приёмника: заменяют тип поля потока перед сверкой."""
        renamed = self._renamed(stream)
        targets = {column.name: column for column in table}
        matches: list[ColumnMatch] = []
        seen: set[str] = set()
        for name, column in renamed:
            seen.add(name)
            matches.append(
                ColumnMatch(
                    name=name,
                    source=self._declared(column, declared.get(name)),
                    target=targets.get(name),
                    rule=self._rules.get(name),
                )
            )

        for column in table:
            if column.name in seen:
                continue

            seen.add(column.name)
            matches.append(
                ColumnMatch(
                    name=column.name,
                    source=None,
                    target=column,
                    rule=self._rules.get(column.name),
                )
            )

        for rule in self._rules.values():
            if rule.name in seen:
                continue

            raise SyncError(
                f"column_types: neither the stream nor the table has a column "
                f"{rule.name!r}; columns: {', '.join(sorted(seen))}"
            )

        return SchemaDiff(matches, self._comparer)

    def _declared(
        self, column: ColumnSpec, declared: DeclaredType | None
    ) -> ColumnSpec:
        if declared is None:
            return column

        return replace(
            column,
            kind=declared.kind,
            source_type=declared.source_type,
            char_length=declared.char_length,
        )

    def _renamed(self, stream: Sequence[ColumnSpec]) -> list[tuple[str, ColumnSpec]]:
        by_source: dict[str, str] = {}
        for rule in self._rules.values():
            if rule.rename_from:
                by_source[rule.rename_from] = rule.name

        names = {column.name for column in stream}
        for source_name in by_source:
            if source_name not in names:
                raise SyncError(
                    f"rename_columns: the stream has no field {source_name!r}; "
                    f"stream fields: {', '.join(sorted(names))}"
                )

        renamed: list[tuple[str, ColumnSpec]] = []
        for column in stream:
            renamed.append((by_source.get(column.name, column.name), column))

        return renamed

    def _rules_of(self, rules: ColumnRules) -> dict[str, ColumnRule]:
        names = set(rules.rename_columns) | set(rules.column_types)
        by_name: dict[str, ColumnRule] = {}
        for name in sorted(names):
            by_name[name] = ColumnRule(
                name=name,
                rename_from=rules.rename_columns.get(name, ""),
                ddl_type=rules.column_types.get(name, ""),
            )

        return by_name


@dataclass(frozen=True)
class DeleteOutcome:
    """Что сделала стратегия удаления: строк и стейтмент."""

    rows: int
    statement: str


class TableDdl(Protocol):
    """Порт таблицы-приёмника: реализация в инфра-пакете движка строит
    стейтменты своим билдером. Имена — идентификаторы движка, условие
    where — текст вызова через raw_query."""

    async def exists(self) -> bool: ...

    async def facts(self) -> Sequence[ColumnSpec]: ...

    async def create(self, spec: TableSpec, unknown_as_varchar: bool) -> str: ...

    async def backup(self) -> str: ...

    async def drop(self, cascade: bool) -> str: ...

    async def truncate(self) -> DeleteOutcome: ...

    async def delete_all(self) -> DeleteOutcome: ...

    async def delete_where(self, where: str) -> DeleteOutcome: ...


class InsertSink(Protocol):
    """Порт вставки потока: load кладёт все пачки потока в колонки таблицы
    по порядку полей потока и возвращает число строк, discard читает поток
    до конца, ничего не записывая."""

    async def load(self, spec: TableSpec) -> int: ...

    async def discard(self) -> int: ...


class SchemaStrategyPlan(Protocol):
    """Стратегия схемы: по наличию таблицы и итогу сверки решает, что с ней
    делать. Реализации — модели с kind (pydantic BaseModel не наследует
    Protocol из-за метакласса, совместимость структурная)."""

    def plan(self, exists: bool, diff: DiffSummary) -> SchemaPlan: ...


class DeleteStrategyApply(Protocol):
    """Стратегия удаления: применяет себя через порт таблицы. Реализации —
    модели с kind."""

    async def apply(self, table: TableDdl) -> DeleteOutcome: ...


class InsertStrategyApply(Protocol):
    """Стратегия вставки: применяет себя через порт вставки. Реализации —
    модели с kind."""

    async def apply(self, sink: InsertSink, spec: TableSpec) -> int: ...


class UnknownTypeApply(Protocol):
    """Стратегия приёмника для колонок семейства other — тех, чей тип он
    сопоставить родному не может (расширения, enum, составные). Известные
    типы всегда ложатся родными типами движка, стратегия решает только
    судьбу неизвестных; явный rules.column_types перекрывает её.
    Реализации — модели с kind."""

    async def apply(self, table: TableDdl, spec: TableSpec) -> str: ...


class FailOnUnknown(BaseModel):
    """UnknownTypeApply: неизвестный тип — ошибка с тем, что о колонке
    известно (имя типа источника или его OID), и подсказкой объявить тип в
    rules.column_types или взять fallback_as_varchar; LLM решает сам."""

    kind: Literal["fail_on_unknown"]

    async def apply(self, table: TableDdl, spec: TableSpec) -> str:
        return await table.create(spec, False)


class FallbackAsVarchar(BaseModel):
    """UnknownTypeApply: неизвестный тип — строковый тип движка без предела
    длины (varchar у postgres, String у ClickHouse), значения едут текстом."""

    kind: Literal["fallback_as_varchar"]

    async def apply(self, table: TableDdl, spec: TableSpec) -> str:
        return await table.create(spec, True)


UnknownTypeStrategy = Annotated[
    FailOnUnknown | FallbackAsVarchar,
    Field(discriminator="kind"),
]
"""Стратегия неизвестных типов: что делать с колонками семейства other."""


class SchemaAction(StrEnum):
    """Что делать с таблицей по стратегии схемы."""

    CREATE = "create"
    BACKUP_THEN_CREATE = "backup_then_create"
    DROP_THEN_CREATE = "drop_then_create"
    KEEP = "keep"
    FAIL = "fail"


@dataclass(frozen=True)
class SchemaPlan:
    action: SchemaAction
    reason: str
    cascade: bool = False


class CreateIfNotExists(BaseModel):
    """SchemaStrategyPlan: нет таблицы — создать по потоку; есть — оставить
    как есть, даже если схема разошлась (расхождение всплывёт при загрузке)."""

    kind: Literal["create_if_not_exists"]

    def plan(self, exists: bool, diff: DiffSummary) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.CREATE, "table is missing")

        return SchemaPlan(SchemaAction.KEEP, "table exists")


class ErrorIfNotExists(BaseModel):
    """SchemaStrategyPlan: таблица обязана быть; схема не сверяется."""

    kind: Literal["error_if_not_exists"]

    def plan(self, exists: bool, diff: DiffSummary) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.FAIL, "table is missing")

        return SchemaPlan(SchemaAction.KEEP, "table exists")


class ErrorIfSchemaChanged(BaseModel):
    """SchemaStrategyPlan: таблица обязана быть и сходиться с потоком без
    ошибок сверки."""

    kind: Literal["error_if_schema_changed"]

    def plan(self, exists: bool, diff: DiffSummary) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.FAIL, "table is missing")

        if diff.changed:
            return SchemaPlan(SchemaAction.FAIL, diff.reason)

        return SchemaPlan(SchemaAction.KEEP, "schema matches")


class DropAndCreateIfSchemaChanged(BaseModel):
    """SchemaStrategyPlan: нет таблицы — создать; схема разошлась — удалить
    и создать заново."""

    kind: Literal["drop_and_create_if_schema_changed"]
    cascade: bool = Field(
        default=False, description="drop ... cascade — снести и зависимые view"
    )

    def plan(self, exists: bool, diff: DiffSummary) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.CREATE, "table is missing")

        if diff.changed:
            return SchemaPlan(SchemaAction.DROP_THEN_CREATE, diff.reason, self.cascade)

        return SchemaPlan(SchemaAction.KEEP, "schema matches")


class BackupAndCreateIfSchemaChanged(BaseModel):
    """SchemaStrategyPlan: нет таблицы — создать; схема разошлась —
    переименовать старую с суффиксом даты и создать новую."""

    kind: Literal["backup_and_create_if_schema_changed"]

    def plan(self, exists: bool, diff: DiffSummary) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.CREATE, "table is missing")

        if diff.changed:
            return SchemaPlan(SchemaAction.BACKUP_THEN_CREATE, diff.reason)

        return SchemaPlan(SchemaAction.KEEP, "schema matches")


class DropAndCreate(BaseModel):
    """SchemaStrategyPlan: всегда пересоздать таблицу по потоку."""

    kind: Literal["drop_and_create"]
    cascade: bool = Field(
        default=False, description="drop ... cascade — снести и зависимые view"
    )

    def plan(self, exists: bool, diff: DiffSummary) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.CREATE, "table is missing")

        return SchemaPlan(SchemaAction.DROP_THEN_CREATE, "table exists", self.cascade)


class BackupAndCreate(BaseModel):
    """SchemaStrategyPlan: всегда старую таблицу — в бэкап с суффиксом даты,
    новую — по потоку."""

    kind: Literal["backup_and_create"]

    def plan(self, exists: bool, diff: DiffSummary) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.CREATE, "table is missing")

        return SchemaPlan(SchemaAction.BACKUP_THEN_CREATE, "table exists")


class DoNothing(BaseModel):
    """SchemaStrategyPlan: таблицу не трогать и не сверять."""

    kind: Literal["do_nothing"]

    def plan(self, exists: bool, diff: DiffSummary) -> SchemaPlan:
        return SchemaPlan(SchemaAction.KEEP, "schema strategy is do_nothing")


SchemaStrategy = Annotated[
    CreateIfNotExists
    | ErrorIfNotExists
    | ErrorIfSchemaChanged
    | DropAndCreateIfSchemaChanged
    | BackupAndCreateIfSchemaChanged
    | DropAndCreate
    | BackupAndCreate
    | DoNothing,
    Field(discriminator="kind"),
]
"""Стратегия схемы: что делать с таблицей-приёмником перед загрузкой."""


class DeleteNothing(BaseModel):
    """DeleteStrategyApply: данные приёмника не трогать."""

    kind: Literal["nothing"]

    async def apply(self, table: TableDdl) -> DeleteOutcome:
        return DeleteOutcome(rows=0, statement="")


class DeleteTruncate(BaseModel):
    """DeleteStrategyApply: truncate table — быстро, без счётчика строк, вне
    транзакции у движков, где truncate не откатывается."""

    kind: Literal["truncate"]

    async def apply(self, table: TableDdl) -> DeleteOutcome:
        return await table.truncate()


class DeleteAll(BaseModel):
    """DeleteStrategyApply: delete from table — все строки, со счётчиком и в
    транзакции."""

    kind: Literal["delete_all"]

    async def apply(self, table: TableDdl) -> DeleteOutcome:
        return await table.delete_all()


class DeleteWhere(BaseModel):
    """DeleteStrategyApply: delete from table where <условие> — условие как в
    SQL приёмника."""

    kind: Literal["delete_where"]
    where: str = Field(
        min_length=1,
        description="Условие после where, как в SQL приёмника: dt >= date '2024-01-01'",
    )

    async def apply(self, table: TableDdl) -> DeleteOutcome:
        return await table.delete_where(self.where)


DeleteStrategy = Annotated[
    DeleteNothing | DeleteTruncate | DeleteAll | DeleteWhere,
    Field(discriminator="kind"),
]
"""Стратегия удаления: что убрать из приёмника перед вставкой."""


class InsertNothing(BaseModel):
    """InsertStrategyApply: поток прочитать до конца и ничего не вставлять."""

    kind: Literal["nothing"]

    async def apply(self, sink: InsertSink, spec: TableSpec) -> int:
        return await sink.discard()


class InsertFull(BaseModel):
    """InsertStrategyApply: все пачки потока — в таблицу как есть."""

    kind: Literal["full"]

    async def apply(self, sink: InsertSink, spec: TableSpec) -> int:
        return await sink.load(spec)


InsertStrategy = Annotated[
    InsertNothing | InsertFull,
    Field(discriminator="kind"),
]
"""Стратегия вставки: как положить поток в приёмник."""


@dataclass(frozen=True)
class SyncReport:
    """Итог приёмника для чата: что сделано со схемой и почему, сверка по
    колонкам, что удалено, сколько вставлено."""

    table: str
    action: SchemaAction
    reason: str
    backup: str
    diff: str
    deleted: DeleteOutcome
    inserted: int
    transactional: bool

    def render(self) -> str:
        lines = [
            f"{self.inserted} rows written into {self.table}",
            f"schema: {self.action.value} ({self.reason})",
        ]
        if self.backup:
            lines.append(f"backup: {self.backup}")

        if self.diff:
            lines.append("columns:")
            lines.append(self.diff)

        if self.deleted.statement:
            lines.append(
                f"deleted: {self.deleted.rows} rows by {self.deleted.statement}"
            )

        if not self.transactional:
            lines.append("note: the steps are not one transaction on this engine")

        return "\n".join(lines)


class WireFormat(StrEnum):
    """Формат тел кадров sync-потока: Arrow IPC (тела подряд — поток IPC)
    или текст COPY postgres в раскладке csv либо text (совместим с
    TabSeparated ClickHouse)."""

    ARROW_IPC = "arrow_ipc"
    PG_COPY_CSV = "pg_copy_csv"
    PG_COPY_TSV = "pg_copy_tsv"


class Engine(StrEnum):
    """Движок на конце sync-потока; UNKNOWN — приёмник не назван, источник
    берёт формат по умолчанию."""

    POSTGRES = "postgres"
    GREENPLUM = "greenplum"
    CLICKHOUSE = "clickhouse"
    ORACLE = "oracle"
    UNKNOWN = "unknown"


class ColumnTypeModel(BaseModel):
    """ColumnType в проводе: те же поля, что у dataclass, но моделью."""

    family: TypeFamily
    text: str = ""
    bits: int = 0
    unsigned: bool = False
    precision: int = 0
    scale: int = 0
    unit: TimeUnit = TimeUnit.MICROSECOND
    zoned: bool = False

    def kind(self) -> ColumnType:
        return ColumnType(
            family=self.family,
            text=self.text,
            bits=self.bits,
            unsigned=self.unsigned,
            precision=self.precision,
            scale=self.scale,
            unit=self.unit,
            zoned=self.zoned,
        )


class StreamColumn(BaseModel):
    """Колонка контракта в проводе: ColumnSpec без позиции — позиция это
    место в списке."""

    name: str
    kind: ColumnTypeModel
    nullable: bool
    source_type: str = ""
    char_length: int = 0

    def spec(self, position: int) -> ColumnSpec:
        return ColumnSpec(
            name=self.name,
            kind=self.kind.kind(),
            nullable=self.nullable,
            position=position,
            source_type=self.source_type,
            char_length=self.char_length,
        )


class ContractText:
    """Текст контракта для отчёта источника: заголовок с форматом провода и
    по строке на колонку — имя, тип, текст типа источника, not null."""

    def render(self, fmt: WireFormat, specs: Sequence[ColumnSpec]) -> str:
        lines = [f"streamed out {fmt.value}, {len(specs)} columns:"]
        for spec in specs:
            lines.append(f"  {spec.name}: {spec.describe()}")

        return "\n".join(lines)


class StreamContract:
    """Перевод контракта между ядром (ColumnSpec) и проводом (StreamColumn)."""

    def columns(self, specs: Sequence[ColumnSpec]) -> list[StreamColumn]:
        columns: list[StreamColumn] = []
        for spec in specs:
            kind = spec.kind
            columns.append(
                StreamColumn(
                    name=spec.name,
                    kind=ColumnTypeModel(
                        family=kind.family,
                        text=kind.text,
                        bits=kind.bits,
                        unsigned=kind.unsigned,
                        precision=kind.precision,
                        scale=kind.scale,
                        unit=kind.unit,
                        zoned=kind.zoned,
                    ),
                    nullable=spec.nullable,
                    source_type=spec.source_type,
                    char_length=spec.char_length,
                )
            )

        return columns

    def specs(self, columns: Sequence[StreamColumn]) -> tuple[ColumnSpec, ...]:
        specs: list[ColumnSpec] = []
        for position, column in enumerate(columns):
            specs.append(column.spec(position))

        return tuple(specs)


class ColumnDeclaration(BaseModel):
    """Декларация LLM для колонки ответа: заданные поля перекрывают то, что
    нашёл драйвер, незаданные остаются его. Тип задаётся структурой
    (семейство и параметры), не текстом."""

    name: str = Field(min_length=1, description="Имя колонки в ответе запроса")
    family: TypeFamily | None = Field(
        default=None,
        description=(
            "integer, float, decimal, boolean, string, binary, date, timestamp, "
            "time, uuid, json, interval, network, bit, money, xml, geometry, "
            "range, textsearch, array, system"
        ),
    )
    bits: int | None = Field(
        default=None, description="Ширина integer/float: 16, 32, 64"
    )
    unsigned: bool | None = Field(default=None, description="Беззнаковое целое")
    precision: int | None = Field(default=None, description="precision у decimal")
    scale: int | None = Field(default=None, description="scale у decimal")
    unit: TimeUnit | None = Field(
        default=None, description="Единица timestamp/time: s, ms, us, ns"
    )
    zoned: bool | None = Field(default=None, description="timestamp с поясом")
    nullable: bool | None = Field(default=None, description="Допустим ли NULL")
    char_length: int | None = Field(
        default=None, description="Длина строки в символах, 0 — без предела"
    )
    source_type: str | None = Field(
        default=None,
        description=(
            "Текст типа в источнике, когда драйвер отдал только OID (enum, "
            "составные, расширения): sales.mood, hstore, vector(3). Приёмник "
            "со стратегией as_is создаёт колонку этим типом."
        ),
    )


class Declarations:
    """Слияние описания драйвера с декларациями LLM: по имени колонки,
    заданное поле декларации перекрывает найденное; смена семейства не
    трогает текст типа источника, его задаёт только source_type."""

    KIND_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"family", "bits", "unsigned", "precision", "scale", "unit", "zoned"}
    )

    def merge(
        self, specs: Sequence[ColumnSpec], declared: Sequence[ColumnDeclaration]
    ) -> tuple[ColumnSpec, ...]:
        by_name = {spec.name: spec for spec in specs}
        for declaration in declared:
            if declaration.name not in by_name:
                raise SyncError(
                    f"columns: the query has no column {declaration.name!r}; "
                    f"columns of the result: {', '.join(by_name)}"
                )

            by_name[declaration.name] = self._applied(
                by_name[declaration.name], declaration
            )

        merged: list[ColumnSpec] = []
        for spec in specs:
            merged.append(by_name[spec.name])

        return tuple(merged)

    def _applied(self, spec: ColumnSpec, declaration: ColumnDeclaration) -> ColumnSpec:
        changed = declaration.model_dump(exclude_none=True, exclude={"name"})
        kind_changes: dict[str, Any] = {}
        for key, value in changed.items():
            if key in self.KIND_FIELDS:
                kind_changes[key] = value

        kind = spec.kind
        if kind_changes:
            family = kind_changes.get("family", kind.family)
            text = kind.text
            if "family" in kind_changes:
                text = family.value

            kind = ColumnType(
                family=family,
                text=text,
                bits=kind_changes.get("bits", kind.bits),
                unsigned=kind_changes.get("unsigned", kind.unsigned),
                precision=kind_changes.get("precision", kind.precision),
                scale=kind_changes.get("scale", kind.scale),
                unit=kind_changes.get("unit", kind.unit),
                zoned=kind_changes.get("zoned", kind.zoned),
            )

        return ColumnSpec(
            name=spec.name,
            kind=kind,
            nullable=changed.get("nullable", spec.nullable),
            position=spec.position,
            source_type=changed.get("source_type", spec.source_type),
            char_length=changed.get("char_length", spec.char_length),
        )


class SchemaFrame(BaseModel):
    """Первый кадр sync-потока: формат тел, движок источника и контракт."""

    kind: Literal["schema"]
    format: WireFormat
    source_engine: Engine
    columns: Sequence[StreamColumn]


class RowsFrame(BaseModel):
    """Кадр данных: тело — блок потока в формате первого кадра."""

    kind: Literal["rows"]


SyncFrame = SchemaFrame | RowsFrame
"""Кадры sync-потока: схема, затем данные; порт различает их по kind."""


class SourceModes(BaseModel):
    """Реестр источника: формат тела по движку приёмника; неназванный или
    неизвестный приёмник получает Arrow."""

    model_config = ConfigDict(frozen=True)

    preferred: Mapping[Engine, WireFormat]

    def format_for(self, target: Engine) -> WireFormat:
        return self.preferred.get(target, WireFormat.ARROW_IPC)


class FrameBodies(io.RawIOBase):
    """Тела кадров подряд как файл на чтение: для читателя Arrow IPC, который
    ждёт файл. Чтение блокирующее, из потока, где стоит читатель."""

    def __init__(self, bodies: Iterator[Chunk]) -> None:
        super().__init__()
        self._bodies = bodies
        self._current = memoryview(b"")

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        target = memoryview(buffer).cast("B")
        while len(self._current) == 0:
            try:
                self._current = memoryview(next(self._bodies))
            except StopIteration:
                return 0

        size = min(len(target), len(self._current))
        target[:size] = self._current[:size]
        self._current = self._current[size:]

        return size


class FrameWriter(io.RawIOBase):
    """Файл на запись поверх выходного порта кадров: каждый write — кадр
    rows с этими байтами. Для писателя Arrow IPC и COPY-блоков из потока."""

    def __init__(self, out: Outbound[SyncFrame]) -> None:
        super().__init__()
        self._out = out

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        view = memoryview(data)
        self._out.emit(RowsFrame(kind="rows"), view)

        return len(view)


class SyncOutbound:
    """Выходной порт sync-потока для async-тел: кадр схемы, кадры данных
    из корутин, файл для писателей, работающих в потоке."""

    def __init__(self, out: Outbound[SyncFrame]) -> None:
        self._out = out

    async def schema(self, frame: SchemaFrame) -> None:
        await asyncio.to_thread(self._out.emit, frame)

    async def rows(self, body: Chunk) -> None:
        await asyncio.to_thread(self._out.emit, RowsFrame(kind="rows"), body)

    def writer(self) -> FrameWriter:
        return FrameWriter(self._out)


class SyncInbound:
    """Входной порт sync-потока для async-тел: первый кадр обязан быть схемой,
    дальше тела данных корутиной или файлом для читателей в потоке."""

    def __init__(self, feed: Inbound[SyncFrame]) -> None:
        self._frames = iter(feed)

    async def schema(self) -> SchemaFrame:
        first = await asyncio.to_thread(next, self._frames, None)
        if first is None:
            raise SyncError("sync stream is empty: expected a schema frame first")

        if not isinstance(first.head, SchemaFrame):
            raise SyncError(
                f"sync stream starts with a {first.head.kind!r} frame, expected schema"
            )

        return first.head

    async def bodies(self) -> AsyncIterator[Chunk]:
        while True:
            frame = await asyncio.to_thread(next, self._frames, None)
            if frame is None:
                return

            yield frame.body

    def raw(self) -> FrameBodies:
        return FrameBodies(self._sync_bodies())

    def _sync_bodies(self) -> Iterator[Chunk]:
        for frame in self._frames:
            yield frame.body
