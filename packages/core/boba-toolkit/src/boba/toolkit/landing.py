"""Приёмник данных со стратегиями: общая часть загрузок между конкретными
движками. Знает только о кадрах провода, о таблице-приёмнике через порт и о
стратегиях схемы, удаления, вставки и неизвестных типов. Типы колонок, их
сверку и DDL описывает пара источник → приёмник в своём пакете, контракт
колонок едет в кадре schema непрозрачным JSON этого источника.

Ошибки:
LandingError — поток не начинается с кадра schema, стратегия схемы отказала,
    колонка неизвестного типа без запасного варианта.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from boba.toolkit.ports import Chunk, Inbound, Outbound

__all__ = [
    "BackupAndCreate",
    "BackupAndCreateIfSchemaChanged",
    "ColumnRules",
    "CreateIfNotExists",
    "DeleteAll",
    "DeleteNothing",
    "DeleteOutcome",
    "DeleteStrategy",
    "DeleteStrategyApply",
    "DeleteTruncate",
    "DeleteWhere",
    "DoNothing",
    "DropAndCreate",
    "DropAndCreateIfSchemaChanged",
    "ErrorIfNotExists",
    "ErrorIfSchemaChanged",
    "FailOnUnknown",
    "FallbackAsVarchar",
    "InsertFull",
    "InsertNothing",
    "InsertStrategy",
    "InsertStrategyApply",
    "LandingError",
    "LandingFrame",
    "LandingInbound",
    "LandingOutbound",
    "LandingReport",
    "LandingRun",
    "LandingSink",
    "LandingTable",
    "RowsHead",
    "SchemaAction",
    "SchemaCheck",
    "SchemaHead",
    "SchemaPlan",
    "SchemaStrategy",
    "SchemaStrategyPlan",
    "SourceEngine",
    "TextWire",
    "UnknownTypeApply",
    "UnknownTypeStrategy",
]


class LandingError(Exception):
    """Нарушение провода или отказ стратегии приёмника."""


class SourceEngine(StrEnum):
    """Движок источника: по нему приёмник берёт пару из реестра."""

    POSTGRES = "postgres"
    CLICKHOUSE = "clickhouse"
    ORACLE = "oracle"


class TextWire(StrEnum):
    """Раскладка текстовых тел кадров: csv — COPY csv postgres; tsv — COPY
    text postgres, байт в байт TabSeparated ClickHouse."""

    CSV = "csv"
    TSV = "tsv"


class SchemaHead(BaseModel):
    """Первый кадр: движок источника, раскладка тел и контракт колонок как
    его описал источник; разбирает контракт пара по source_engine."""

    kind: Literal["schema"]
    source_engine: SourceEngine
    wire: TextWire
    contract: JsonValue


class RowsHead(BaseModel):
    """Кадр данных: тело — блок строк в раскладке первого кадра."""

    kind: Literal["rows"]


LandingFrame = SchemaHead | RowsHead
"""Кадры провода: схема, затем данные; порт различает их по kind."""


class LandingOutbound:
    """Выходной порт провода для async-тел: кадр схемы и кадры данных из
    корутин, эмиссия в потоке."""

    def __init__(self, out: Outbound[LandingFrame]) -> None:
        self._out = out

    async def schema(self, head: SchemaHead) -> None:
        await asyncio.to_thread(self._out.emit, head)

    async def rows(self, body: Chunk) -> None:
        await asyncio.to_thread(self._out.emit, RowsHead(kind="rows"), body)


class LandingInbound:
    """Входной порт провода для async-тел: первый кадр обязан быть схемой,
    дальше тела данных корутиной."""

    def __init__(self, feed: Inbound[LandingFrame]) -> None:
        self._frames = iter(feed)

    async def get_schema(self) -> SchemaHead:
        first = await asyncio.to_thread(next, self._frames, None)
        if first is None:
            raise LandingError("landing stream is empty: expected a schema frame first")

        if not isinstance(first.head, SchemaHead):
            raise LandingError(
                f"landing stream starts with a {first.head.kind!r} frame, "
                f"expected schema"
            )

        return first.head

    async def bodies(self) -> AsyncIterator[Chunk]:
        while True:
            frame = await asyncio.to_thread(next, self._frames, None)
            if frame is None:
                return

            yield frame.body

    def sync_bodies(self) -> Iterator[Chunk]:
        for frame in self._frames:
            yield frame.body


class ColumnRules(BaseModel):
    """Правила приёмника по колонкам: rename_columns — откуда берётся колонка,
    column_types — какой тип ей дать в DDL текстом приёмника."""

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
            "Колонка приёмника -> тип для create table текстом приёмника как "
            'есть: {"v": "vector(3)", "amount": "numeric(20,6)"}. Перекрывает '
            "тип из контракта и стратегию unknown_types."
        ),
    )


@dataclass(frozen=True)
class DeleteOutcome:
    """Что сделала стратегия удаления: строк и стейтмент."""

    rows: int
    statement: str


@dataclass(frozen=True)
class SchemaCheck:
    """Итог сверки контракта с существующей таблицей от пары: ошибки не дают
    загрузить без пересоздания, предупреждения — нет; lines — по строке на
    колонку для отчёта."""

    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    lines: tuple[str, ...]

    def changed(self) -> bool:
        return bool(self.errors)

    def reason(self) -> str:
        return "; ".join(self.errors)

    def render(self) -> str:
        return "\n".join(self.lines)


class LandingTable(Protocol):
    """Порт таблицы-приёмника: реализация в паре строит стейтменты билдером
    своего движка. create знает колонки из плана пары; unknown_as_varchar —
    колонки без типа получают строковый тип движка, иначе отказ."""

    async def exists(self) -> bool: ...

    async def create(self, unknown_as_varchar: bool) -> str: ...

    async def backup(self) -> str: ...

    async def drop(self, cascade: bool) -> str: ...

    async def truncate(self) -> DeleteOutcome: ...

    async def delete_all(self) -> DeleteOutcome: ...

    async def delete_where(self, where: str) -> DeleteOutcome: ...


class LandingSink(Protocol):
    """Порт вставки: load кладёт все тела потока в таблицу и возвращает число
    строк, discard читает поток до конца, ничего не записывая."""

    async def load(self) -> int: ...

    async def discard(self) -> int: ...


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


class SchemaStrategyPlan(Protocol):
    """Стратегия схемы: по наличию таблицы и итогу сверки решает, что с ней
    делать. Реализации — модели с kind (pydantic BaseModel не наследует
    Protocol из-за метакласса, совместимость структурная)."""

    def plan(self, exists: bool, check: SchemaCheck) -> SchemaPlan: ...


class DeleteStrategyApply(Protocol):
    """Стратегия удаления: применяет себя через порт таблицы. Реализации —
    модели с kind."""

    async def apply(self, table: LandingTable) -> DeleteOutcome: ...


class InsertStrategyApply(Protocol):
    """Стратегия вставки: применяет себя через порт вставки. Реализации —
    модели с kind."""

    async def apply(self, sink: LandingSink) -> int: ...


class UnknownTypeApply(Protocol):
    """Стратегия для колонок, тип которых пара сопоставить не может; явный
    rules.column_types перекрывает её. Реализации — модели с kind."""

    async def apply(self, table: LandingTable) -> str: ...


class CreateIfNotExists(BaseModel):
    """SchemaStrategyPlan: нет таблицы — создать по контракту; есть — оставить
    как есть, даже если схема разошлась (расхождение всплывёт при загрузке)."""

    kind: Literal["create_if_not_exists"]

    def plan(self, exists: bool, check: SchemaCheck) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.CREATE, "table is missing")

        return SchemaPlan(SchemaAction.KEEP, "table exists")


class ErrorIfNotExists(BaseModel):
    """SchemaStrategyPlan: таблица обязана быть; схема не сверяется."""

    kind: Literal["error_if_not_exists"]

    def plan(self, exists: bool, check: SchemaCheck) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.FAIL, "table is missing")

        return SchemaPlan(SchemaAction.KEEP, "table exists")


class ErrorIfSchemaChanged(BaseModel):
    """SchemaStrategyPlan: таблица обязана быть и сходиться с контрактом без
    ошибок сверки."""

    kind: Literal["error_if_schema_changed"]

    def plan(self, exists: bool, check: SchemaCheck) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.FAIL, "table is missing")

        if check.changed():
            return SchemaPlan(SchemaAction.FAIL, check.reason())

        return SchemaPlan(SchemaAction.KEEP, "schema matches")


class DropAndCreateIfSchemaChanged(BaseModel):
    """SchemaStrategyPlan: нет таблицы — создать; схема разошлась — удалить
    и создать заново."""

    kind: Literal["drop_and_create_if_schema_changed"]
    cascade: bool = Field(
        default=False, description="drop ... cascade — снести и зависимые view"
    )

    def plan(self, exists: bool, check: SchemaCheck) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.CREATE, "table is missing")

        if check.changed():
            return SchemaPlan(
                SchemaAction.DROP_THEN_CREATE, check.reason(), self.cascade
            )

        return SchemaPlan(SchemaAction.KEEP, "schema matches")


class BackupAndCreateIfSchemaChanged(BaseModel):
    """SchemaStrategyPlan: нет таблицы — создать; схема разошлась —
    переименовать старую с суффиксом даты и создать новую."""

    kind: Literal["backup_and_create_if_schema_changed"]

    def plan(self, exists: bool, check: SchemaCheck) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.CREATE, "table is missing")

        if check.changed():
            return SchemaPlan(SchemaAction.BACKUP_THEN_CREATE, check.reason())

        return SchemaPlan(SchemaAction.KEEP, "schema matches")


class DropAndCreate(BaseModel):
    """SchemaStrategyPlan: всегда пересоздать таблицу по контракту."""

    kind: Literal["drop_and_create"]
    cascade: bool = Field(
        default=False, description="drop ... cascade — снести и зависимые view"
    )

    def plan(self, exists: bool, check: SchemaCheck) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.CREATE, "table is missing")

        return SchemaPlan(SchemaAction.DROP_THEN_CREATE, "table exists", self.cascade)


class BackupAndCreate(BaseModel):
    """SchemaStrategyPlan: всегда старую таблицу — в бэкап с суффиксом даты,
    новую — по контракту."""

    kind: Literal["backup_and_create"]

    def plan(self, exists: bool, check: SchemaCheck) -> SchemaPlan:
        if not exists:
            return SchemaPlan(SchemaAction.CREATE, "table is missing")

        return SchemaPlan(SchemaAction.BACKUP_THEN_CREATE, "table exists")


class DoNothing(BaseModel):
    """SchemaStrategyPlan: таблицу не трогать и не сверять."""

    kind: Literal["do_nothing"]

    def plan(self, exists: bool, check: SchemaCheck) -> SchemaPlan:
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

    async def apply(self, table: LandingTable) -> DeleteOutcome:
        return DeleteOutcome(rows=0, statement="")


class DeleteTruncate(BaseModel):
    """DeleteStrategyApply: truncate table — быстро, без счётчика строк, вне
    транзакции у движков, где truncate не откатывается."""

    kind: Literal["truncate"]

    async def apply(self, table: LandingTable) -> DeleteOutcome:
        return await table.truncate()


class DeleteAll(BaseModel):
    """DeleteStrategyApply: delete from table — все строки, со счётчиком и в
    транзакции."""

    kind: Literal["delete_all"]

    async def apply(self, table: LandingTable) -> DeleteOutcome:
        return await table.delete_all()


class DeleteWhere(BaseModel):
    """DeleteStrategyApply: delete from table where <условие> — условие как в
    SQL приёмника."""

    kind: Literal["delete_where"]
    where: str = Field(
        min_length=1,
        description="Условие после where, как в SQL приёмника: dt >= date '2024-01-01'",
    )

    async def apply(self, table: LandingTable) -> DeleteOutcome:
        return await table.delete_where(self.where)


DeleteStrategy = Annotated[
    DeleteNothing | DeleteTruncate | DeleteAll | DeleteWhere,
    Field(discriminator="kind"),
]
"""Стратегия удаления: что убрать из приёмника перед вставкой."""


class InsertNothing(BaseModel):
    """InsertStrategyApply: поток прочитать до конца и ничего не вставлять."""

    kind: Literal["nothing"]

    async def apply(self, sink: LandingSink) -> int:
        return await sink.discard()


class InsertFull(BaseModel):
    """InsertStrategyApply: все тела потока — в таблицу как есть."""

    kind: Literal["full"]

    async def apply(self, sink: LandingSink) -> int:
        return await sink.load()


InsertStrategy = Annotated[
    InsertNothing | InsertFull,
    Field(discriminator="kind"),
]
"""Стратегия вставки: как положить поток в приёмник."""


class FailOnUnknown(BaseModel):
    """UnknownTypeApply: колонка без типа у приёмника — ошибка с тем, что о
    ней известно, и подсказкой объявить тип в rules.column_types или взять
    fallback_as_varchar; LLM решает сам."""

    kind: Literal["fail_on_unknown"]

    async def apply(self, table: LandingTable) -> str:
        return await table.create(False)


class FallbackAsVarchar(BaseModel):
    """UnknownTypeApply: колонка без типа у приёмника — строковый тип движка
    без предела длины (varchar у postgres, String у ClickHouse)."""

    kind: Literal["fallback_as_varchar"]

    async def apply(self, table: LandingTable) -> str:
        return await table.create(True)


UnknownTypeStrategy = Annotated[
    FailOnUnknown | FallbackAsVarchar,
    Field(discriminator="kind"),
]
"""Стратегия неизвестных типов: что делать с колонками без типа у приёмника."""


@dataclass(frozen=True)
class LandingReport:
    """Итог приёмника для чата: что сделано со схемой и почему, сверка по
    колонкам, что удалено, сколько вставлено."""

    table: str
    action: SchemaAction
    reason: str
    backup: str
    check: str
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

        if self.check:
            lines.append("columns:")
            lines.append(self.check)

        if self.deleted.statement:
            lines.append(
                f"deleted: {self.deleted.rows} rows by {self.deleted.statement}"
            )

        if not self.transactional:
            lines.append("note: the steps are not one transaction on this engine")

        return "\n".join(lines)


class LandingRun:
    """Общий ход приёмника после сверки: план стратегии схемы и его действия
    через порт таблицы, удаление, вставка, отчёт. Транзакцию и сверку делает
    пара до вызова."""

    def __init__(
        self,
        schema_strategy: SchemaStrategyPlan,
        delete_strategy: DeleteStrategyApply,
        insert_strategy: InsertStrategyApply,
        unknown_types: UnknownTypeApply,
    ) -> None:
        self._schema_strategy = schema_strategy
        self._delete_strategy = delete_strategy
        self._insert_strategy = insert_strategy
        self._unknown_types = unknown_types

    async def run(  # noqa: PLR0913
        self,
        table_name: str,
        exists: bool,
        check: SchemaCheck,
        table: LandingTable,
        sink: LandingSink,
        transactional: bool,
    ) -> LandingReport:
        plan = self._schema_strategy.plan(exists, check)
        backup = ""
        match plan.action:
            case SchemaAction.CREATE:
                await self._unknown_types.apply(table)
            case SchemaAction.BACKUP_THEN_CREATE:
                backup = await table.backup()
                await self._unknown_types.apply(table)
            case SchemaAction.DROP_THEN_CREATE:
                await table.drop(plan.cascade)
                await self._unknown_types.apply(table)
            case SchemaAction.KEEP:
                pass
            case SchemaAction.FAIL:
                raise LandingError(
                    f"schema strategy refused {table_name}: {plan.reason}\n"
                    f"{check.render()}"
                )

        deleted = await self._delete_strategy.apply(table)
        inserted = await self._insert_strategy.apply(sink)

        check_text = ""
        if exists:
            check_text = check.render()

        return LandingReport(
            table=table_name,
            action=plan.action,
            reason=plan.reason,
            backup=backup,
            check=check_text,
            deleted=deleted,
            inserted=inserted,
            transactional=transactional,
        )
