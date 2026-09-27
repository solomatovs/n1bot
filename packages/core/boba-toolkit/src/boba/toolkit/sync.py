"""Нейтральный контракт колонок для провода arrow: семейства типов и их
параметры, декларации LLM поверх описания драйвера, сверка контракта со
схемой таблицы-приёмника по семействам. Ничего не знает ни о pyarrow, ни о
драйверах: поле потока сюда переводит boba.toolkit.arrow, колонку каталога
— модуль движка; стратегии, кадры и ход приёмника — в boba.toolkit.transfer.

Ошибки:
SyncError — правило вызова не сходится со схемами (rename на поле,
    которого нет в потоке) или стратегия отказала (FAIL с причиной).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from boba.toolkit.transfer import ColumnRules, SchemaCheck, TransferError

__all__ = [
    "ArrowContract",
    "ArrowFieldMeta",
    "ColumnDeclaration",
    "ColumnMatch",
    "ColumnRule",
    "ColumnSpec",
    "ColumnType",
    "ColumnTypeModel",
    "ColumnVerdict",
    "ContractText",
    "Declarations",
    "DeclaredType",
    "Engine",
    "SchemaDiff",
    "SchemaMatcher",
    "StreamColumn",
    "StreamContract",
    "SyncError",
    "TableColumn",
    "TableSpec",
    "TimeUnit",
    "TypeComparer",
    "TypeFamily",
    "Verdict",
]


class SyncError(TransferError):
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
        - семейство типов совпадает; целое ложится и в decimal без дробной
            части, когда разрядов хватает на его величину
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

    INTEGER_DIGITS: ClassVar[Mapping[tuple[int, bool], int]] = {
        (1, False): 1,
        (1, True): 1,
        (8, False): 3,
        (8, True): 3,
        (16, False): 5,
        (16, True): 5,
        (32, False): 10,
        (32, True): 10,
        (64, False): 19,
        (64, True): 20,
    }
    """Десятичных разрядов хватает на любое целое такой ширины и знака."""

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
        if src.family is TypeFamily.INTEGER and tgt.family is TypeFamily.DECIMAL:
            return self._integer_into_decimal(src, tgt)

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

    def _integer_into_decimal(self, src: ColumnType, tgt: ColumnType) -> ColumnVerdict:
        """Целое в decimal без дробной части: у движка без целых типов
        (Oracle NUMBER(19)) это единственная колонка под int64. Разрядов
        приёмника должно хватить на величину целого; без точности — хватит."""
        if tgt.scale != 0:
            return ColumnVerdict(
                Verdict.ERROR,
                f"type family differs: stream {src.text}, table {tgt.text}",
            )

        if tgt.precision == 0:
            return ColumnVerdict(Verdict.OK, "ok")

        digits = self.INTEGER_DIGITS.get((src.bits, src.unsigned), 0)
        if digits == 0:
            return ColumnVerdict(
                Verdict.ERROR,
                f"type family differs: stream {src.text}, table {tgt.text}",
            )

        if digits > tgt.precision:
            return ColumnVerdict(
                Verdict.ERROR,
                f"table {tgt.text} holds fewer integer digits than stream {src.text}",
            )

        if digits < tgt.precision:
            return ColumnVerdict(
                Verdict.WARNING,
                f"table {tgt.text} is wider than stream {src.text}",
            )

        return ColumnVerdict(Verdict.OK, "ok")

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

    def check(self) -> SchemaCheck:
        """Итог сверки для стратегии приёмника."""
        return SchemaCheck(
            errors=tuple(self.errors()),
            warnings=tuple(self.warnings()),
            lines=tuple(self._lines()),
        )

    def render(self) -> str:
        return "\n".join(self._lines())

    def _lines(self) -> Iterator[str]:
        for match, verdict in self._verdicts:
            yield f"- {verdict.level.value} {match.name}: {verdict.message}"

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


class Engine(StrEnum):
    """Движок источника нейтрального контракта: у postgres приёмник берёт
    текст типа как есть, у остальных выводит тип из семейства."""

    POSTGRES = "postgres"
    GREENPLUM = "greenplum"
    CLICKHOUSE = "clickhouse"
    ORACLE = "oracle"


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

    def render(self, wire: str, specs: Sequence[ColumnSpec]) -> str:
        lines = [f"streamed out {wire}, {len(specs)} columns:"]
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


class ArrowContract(BaseModel):
    """Нейтральный контракт в кадре schema провода arrow: колонки потока
    для приёмника без пары."""

    columns: Sequence[StreamColumn]


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
