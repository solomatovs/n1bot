"""Общий цикл скраперов каталога: процесс на источник, внутри всё по порядку.

Родитель читает конфиг и раздаёт источники процессам: по одному источнику на
процесс, процесс умирает вместе с прогоном. Внутри одна попытка это одно выделенное
соединение к ix и одна сессия источника: файлы scrape/ по волнам, строки каждого
потоком в temp raw_<name> через COPY, сверка перечитыванием и except all на стороне
ix, стадии layout/ в autocommit, advisory-замок на scope, apply одной транзакцией
repeatable read. Каталог изменился во время чтения или ix занят — попытка
повторяется с новыми сессиями, temp-таблицы прежней умирают вместе с ней.

Источник (PostgreSQL, ClickHouse, Oracle) даёт реализацию ScrapeSource: объявить
свои файлы scrape/ моделями ScrapeFile, открыть сессию, сказать, подходит ли файл
серверу, прочитать файл своим билдером и отдать результат байтами в формате COPY
PostgreSQL (text или csv), назвать свой адрес для raw_source. Строки в Python не
собираются: блок байт от драйвера источника уходит в COPY ix как есть; PostgreSQL
отдаёт его через `COPY ... TO STDOUT`, ClickHouse через TabSeparated, Oracle через
Arrow-пачки и CSV. В файле лежит только запрос, сверка лежит рядом в
`<файл>.verify.sql`; ядро текст файлов не разбирает. Массивы collect для запросов
следующих волн читаются из raw-таблицы на стороне ix после COPY.

Ошибки:
ScrapeWorkerError — ix недоступен, контракт файлов нарушен, источник отказал,
    попытки исчерпаны, процесс источника упал не своей ошибкой.
ScrapeSourceError — выпускает реализация источника: источник недоступен или
    отклонил запрос.
ScrapeSourceBusyError — отказ источника временный, попытка повторяется.
CatalogChangedError — каталог источника изменился между чтением и сверкой.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import multiprocessing
from abc import abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

import psycopg
from psycopg import sql
from psycopg.errors import LockNotAvailable, SerializationFailure
from pydantic import BaseModel, Field

from boba.config import ConfigError, bind_section
from boba.db.postgres import AsyncPostgresPool, PostgresError
from boba.db.postgres.query import PgQueryBuilder
from boba.ix_core.database import IxDatabase, enter_kerberos
from boba.ix_core.upgrade import SchemaUpgrade, SchemaUpgradeError
from boba.krb import KerberosWorkspaceConfig

__all__ = [
    "ApplyRow",
    "BlockStream",
    "CatalogChangedError",
    "Collect",
    "CopyFormat",
    "LayoutFile",
    "PackageDir",
    "ScrapeFile",
    "ScrapeReport",
    "ScrapeSession",
    "ScrapeSource",
    "ScrapeSourceBusyError",
    "ScrapeSourceError",
    "ScrapeWorkerError",
    "ScraperConfigBase",
    "SourceAddressBase",
    "SourceBlocks",
    "SourceConfigBase",
    "VersionGate",
    "parse_version",
    "run_cli",
    "run_sources",
    "scrape_in_process",
    "scrape_source",
]

logger = logging.getLogger("ix-scrape")


class ScrapeWorkerError(Exception):
    """Ошибка прогона скрапера."""


class ScrapeSourceError(Exception):
    """Источник недоступен или отклонил запрос; поднимает реализация источника."""


class ScrapeSourceBusyError(ScrapeSourceError):
    """Временный отказ источника (замок, сериализация): попытка повторяется."""


class CatalogChangedError(Exception):
    """Каталог источника изменился между чтением и сверкой."""


class PackageDir(StrEnum):
    """Каталоги пакета скрапера рядом с worker.py."""

    SCHEMA = "schema"
    SCRAPE = "scrape"
    LAYOUT = "layout"


class LayoutFile(StrEnum):
    RAW_SCHEMA = "00_raw_schema.sql"
    STAGE = "10_stage.sql"
    NODES = "20_nodes.sql"
    TREE = "30_tree.sql"
    EDGES = "40_edges.sql"
    SURFACES = "45_surfaces.sql"
    LOCK = "48_lock.sql"
    APPLY = "50_apply.sql"
    UNLOCK = "55_unlock.sql"


@dataclass(frozen=True, kw_only=True)
class Collect:
    """Массив для запросов следующих волн: имя массива и колонка результата."""

    name: str
    column: str


@dataclass(frozen=True, kw_only=True)
class VersionGate:
    """Ворота файла по серверу. Версия сравнивается по длине ворот: max (19,)
    отсекает 21.0, но пропускает 19.3; only и unless сравнивают вкус сервера
    (у PostgreSQL это gp для Greenplum). Базовый класс ScrapeFile и файлов DDL
    стендов."""

    min_version: tuple[int, ...] = ()
    max_version: tuple[int, ...] = ()
    only: str = ""
    unless: str = ""

    def applies(self, version: Sequence[int], flavor: str) -> bool:
        low = self.min_version
        if low and tuple(version[: len(low)]) < low:
            return False

        high = self.max_version
        if high and tuple(version[: len(high)]) > high:
            return False

        if self.only and flavor != self.only:
            return False

        return not (self.unless and flavor == self.unless)


@dataclass(frozen=True, kw_only=True)
class ScrapeFile(VersionGate):
    """Объявление файла scrape/ источником: имя raw-таблицы, волна, файл запроса,
    массивы параметров и ворота. Сверка лежит рядом с запросом в `<файл>.verify.sql`."""

    name: str
    wave: int
    query: str
    params: tuple[str, ...] = ()
    collect: Collect | None = None

    def verify(self) -> str:
        return f"{Path(self.query).stem}.verify.sql"

    def raw_table(self) -> str:
        return f"raw_{self.name}"

    def verify_table(self) -> str:
        return f"verify_{self.name}"


@dataclass(frozen=True, kw_only=True)
class ApplyRow:
    op: str
    planned: int
    applied: int


@dataclass(frozen=True, kw_only=True)
class ScrapeReport:
    """Итог прогона одного источника: сводка apply и число попыток."""

    source: str
    rows: Sequence[ApplyRow]
    attempts: int

    def applied(self) -> int:
        total = 0
        for row in self.rows:
            total += row.applied

        return total

    def line(self) -> str:
        parts: list[str] = []
        for row in self.rows:
            if row.applied:
                parts.append(f"{row.op}={row.applied}")

        changed = " ".join(parts)
        if not changed:
            changed = "no changes"

        return f"{self.source}: {changed}, attempts={self.attempts}"


@dataclass(frozen=True, kw_only=True)
class SourceAddressBase:
    """Адрес источника для raw_source: поля модели это колонки таблицы. Реализация
    добавляет свои (у PostgreSQL база), совпадающие с raw_source её layout."""

    scheme: str
    host: str
    port: int

    def columns(self) -> dict[str, object]:
        return asdict(self)


class CopyFormat(StrEnum):
    """Формат блоков источника для COPY ... FROM STDIN: значение это список опций
    COPY. NULL в text это `\\N`, в csv — пустое поле без кавычек."""

    TEXT = "format text"
    CSV = "format csv, null ''"


class SourceBlocks(Protocol):
    """Результат одного запроса к источнику: имена колонок, формат COPY и блоки
    байт потоком."""

    @property
    @abstractmethod
    def columns(self) -> Sequence[str]: ...

    @property
    @abstractmethod
    def copy_format(self) -> CopyFormat: ...

    @abstractmethod
    def __aiter__(self) -> AsyncIterator[memoryview]: ...


class ScrapeSession(Protocol):
    """Открытая сессия источника на одну попытку прогона."""

    @abstractmethod
    def applies(self, file: ScrapeFile) -> bool:
        """Подходит ли файл серверу этой сессии."""

    @abstractmethod
    def fetch_blocks(
        self, name: str, path: Path, params: Mapping[str, Sequence[object]]
    ) -> AbstractAsyncContextManager[SourceBlocks]:
        """Результат запроса из файла path блоками байт под именем name; массивы по
        именам params объявления."""


class ScrapeSource(Protocol):
    """Источник каталога: файлы scrape/, адрес для raw_source и сессия на попытку."""

    @property
    @abstractmethod
    def files(self) -> Sequence[ScrapeFile]: ...

    @property
    @abstractmethod
    def address(self) -> SourceAddressBase: ...

    @abstractmethod
    def describe(self) -> str:
        """Подпись источника для сообщений об ошибках."""

    @abstractmethod
    def open_session(self) -> AbstractAsyncContextManager[ScrapeSession]: ...


class BlockStream(SourceBlocks):
    """Блоки байт от драйвера источника как memoryview на его буфер, без копии;
    отказ сервера по дороге уходит ScrapeSourceError."""

    def __init__(
        self,
        columns: Sequence[str],
        copy_format: CopyFormat,
        blocks: AsyncIterator[memoryview],
        label: str,
        errors: tuple[type[Exception], ...],
    ) -> None:
        self._columns = tuple(columns)
        self._format = copy_format
        self._blocks = blocks
        self._label = label
        self._errors = errors

    @property
    def columns(self) -> Sequence[str]:
        return self._columns

    @property
    def copy_format(self) -> CopyFormat:
        return self._format

    async def __aiter__(self) -> AsyncIterator[memoryview]:
        try:
            async for block in self._blocks:
                yield block
        except self._errors as exc:
            raise ScrapeSourceError(f"reading {self._label}: {exc}") from exc


def parse_version(raw: str) -> tuple[int, ...]:
    """Версия сервера кортежем чисел: 12.2.0.1.0 -> (12, 2, 0, 1, 0)."""
    parts: list[int] = []
    for piece in raw.strip().split("."):
        if not piece.isdigit():
            raise ScrapeSourceError(
                f"server version: expected dotted numbers, got {raw!r}"
            )

        parts.append(int(piece))

    if not parts:
        raise ScrapeSourceError(f"server version: expected dotted numbers, got {raw!r}")

    return tuple(parts)


def choose_files(
    files: Sequence[ScrapeFile], session: ScrapeSession, where: str
) -> list[ScrapeFile]:
    """По одному варианту на имя, в порядке волн; два подходящих варианта — ошибка."""
    by_name: dict[str, list[ScrapeFile]] = {}
    for file in files:
        by_name.setdefault(file.name, []).append(file)

    chosen: list[ScrapeFile] = []
    for name in sorted(by_name, key=lambda n: (by_name[n][0].wave, n)):
        variants: list[ScrapeFile] = []
        for file in by_name[name]:
            if session.applies(file):
                variants.append(file)

        if len(variants) > 1:
            listed = ", ".join(file.query for file in variants)
            raise ScrapeWorkerError(
                f"scrape {name}: {len(variants)} variants apply to {where}: {listed}"
            )

        if variants:
            chosen.append(variants[0])

    return chosen


def pick_params(
    arrays: Mapping[str, Sequence[object]], names: Sequence[str]
) -> dict[str, list[object]]:
    values: dict[str, list[object]] = {}
    for name in names:
        values[name] = list(arrays.get(name, ()))

    return values


async def register_source(
    ix: psycopg.AsyncConnection[Any], address: SourceAddressBase
) -> None:
    columns = address.columns()
    names: list[sql.Composable] = []
    marks: list[sql.Composable] = []
    for name in columns:
        names.append(sql.Identifier(name))
        marks.append(sql.Placeholder(name))

    query = (
        PgQueryBuilder()
        .add(
            "insert into {table} ({names}) values ({marks})",
            table=sql.Identifier("raw_source"),
            names=sql.SQL(", ").join(names),
            marks=sql.SQL(", ").join(marks),
            **columns,
        )
        .build()
    )
    await ix.execute(query.text, query.params)


async def copy_blocks(
    ix: psycopg.AsyncConnection[Any], target: str, blocks: SourceBlocks
) -> None:
    """Блоки источника в temp-таблицу ix одним COPY, колонки в порядке источника."""
    names: list[sql.Composable] = []
    for column in blocks.columns:
        names.append(sql.Identifier(column))

    statement = (
        PgQueryBuilder()
        .add(
            "copy {target} ({names}) from stdin ({options})",
            target=sql.Identifier(target),
            names=sql.SQL(", ").join(names),
            options=sql.SQL(blocks.copy_format.value),
        )
        .build()
    )
    async with ix.cursor().copy(statement.text) as copy:
        async for block in blocks:
            await copy.write(block)


async def copy_rows(
    session: ScrapeSession,
    ix: psycopg.AsyncConnection[Any],
    file: ScrapeFile,
    scrape_dir: Path,
    arrays: Mapping[str, Sequence[object]],
) -> dict[str, Sequence[object]]:
    """Выборка одного файла блоками в raw_<name>; массив collect читается из неё."""
    values = pick_params(arrays, file.params)
    async with session.fetch_blocks(
        file.name, scrape_dir / file.query, values
    ) as blocks:
        await copy_blocks(ix, file.raw_table(), blocks)

    query = (
        PgQueryBuilder()
        .add("select count(*) from {raw}", raw=sql.Identifier(file.raw_table()))
        .build()
    )
    cur = await ix.execute(query.text, query.params)
    counted = await cur.fetchone()
    if counted is None:
        raise ScrapeWorkerError(f"scrape {file.name}: expected a count, got none")

    logger.info("scrape %s (%s): %d rows", file.name, file.query, int(counted[0]))

    if file.collect is None:
        return {}

    query = (
        PgQueryBuilder()
        .add(
            "select {column} from {raw}",
            column=sql.Identifier(file.collect.column),
            raw=sql.Identifier(file.raw_table()),
        )
        .build()
    )
    cur = await ix.execute(query.text, query.params)
    collected: list[object] = []
    async for row in cur:
        collected.append(row[0])

    return {file.collect.name: collected}


async def verify_rows(
    session: ScrapeSession,
    ix: psycopg.AsyncConnection[Any],
    file: ScrapeFile,
    scrape_dir: Path,
    arrays: Mapping[str, Sequence[object]],
) -> None:
    """Сверка: `<файл>.verify.sql` блоками в verify_<name>, затем except all с
    raw_<name> в обе стороны на стороне ix."""
    values = pick_params(arrays, file.params)
    raw = sql.Identifier(file.raw_table())
    check = sql.Identifier(file.verify_table())
    async with session.fetch_blocks(
        file.verify_table(), scrape_dir / file.verify(), values
    ) as blocks:
        names: list[sql.Composable] = []
        for column in blocks.columns:
            names.append(sql.Identifier(column))

        keys = sql.SQL(", ").join(names)
        query = (
            PgQueryBuilder()
            .add(
                "create temp table {c} as select {k} from {r} where false",
                c=check,
                k=keys,
                r=raw,
            )
            .build()
        )
        await ix.execute(query.text, query.params)
        await copy_blocks(ix, file.verify_table(), blocks)

    query = (
        PgQueryBuilder(k=keys, r=raw, c=check)
        .add("select count(*) from (")
        .add("(select {k} from {r} except all select {k} from {c})")
        .add("union all (select {k} from {c} except all select {k} from {r})")
        .add(") d")
        .build()
    )
    diff_cur = await ix.execute(query.text, query.params)
    diff = await diff_cur.fetchone()
    query = PgQueryBuilder().add("drop table {c}", c=check).build()
    await ix.execute(query.text, query.params)

    if diff is None:
        raise ScrapeWorkerError(f"verify {file.name}: expected a count, got none")

    if int(diff[0]) != 0:
        raise CatalogChangedError(file.name)


async def read_last_rows(cur: psycopg.AsyncCursor[Any]) -> list[tuple[object, ...]]:
    """Скрипт из многих statement'ов: сводка это последний набор строк."""
    rows: list[tuple[object, ...]] = []
    while True:
        if cur.description is not None:
            rows = await cur.fetchall()

        if not cur.nextset():
            return rows


async def apply_layout(
    ix: psycopg.AsyncConnection[Any], layout_dir: Path, db_schema: str
) -> list[ApplyRow]:
    """Замок на scope, apply одной транзакцией repeatable read, замок снят."""
    try:
        query = (
            PgQueryBuilder(schema=sql.Identifier(db_schema))
            .read(layout_dir / LayoutFile.LOCK)
            .build()
        )
        await ix.execute(query.text, query.params)
        await ix.execute("begin isolation level repeatable read")
        try:
            query = (
                PgQueryBuilder(schema=sql.Identifier(db_schema))
                .read(layout_dir / LayoutFile.APPLY)
                .build()
            )
            cur = await ix.execute(query.text, query.params)
            rows = await read_last_rows(cur)
            await ix.execute("commit")
        except Exception:
            await ix.execute("rollback")
            raise
    finally:
        query = (
            PgQueryBuilder(schema=sql.Identifier(db_schema))
            .read(layout_dir / LayoutFile.UNLOCK)
            .build()
        )
        await ix.execute(query.text, query.params)

    summary: list[ApplyRow] = []
    for row in rows:
        summary.append(
            ApplyRow(op=str(row[0]), planned=int(str(row[1])), applied=int(str(row[2])))
        )

    return summary


async def scrape_once(
    database: IxDatabase, source: ScrapeSource, package_dir: Path
) -> list[ApplyRow]:
    """Одна попытка: raw-таблицы, строки всех файлов, сверка, стадии, apply."""
    schema = database.db_schema
    scrape_dir = package_dir / PackageDir.SCRAPE
    layout_dir = package_dir / PackageDir.LAYOUT
    async with (
        await AsyncPostgresPool.dedicated(database.postgres.copy_text()) as ix,
        source.open_session() as session,
    ):
        chosen = choose_files(source.files, session, source.describe())

        query = (
            PgQueryBuilder(schema=sql.Identifier(schema))
            .read(layout_dir / LayoutFile.RAW_SCHEMA)
            .build()
        )
        await ix.execute(query.text, query.params)
        await register_source(ix, source.address)

        arrays: dict[str, Sequence[object]] = {}
        for file in chosen:
            arrays.update(await copy_rows(session, ix, file, scrape_dir, arrays))

        for file in chosen:
            await verify_rows(session, ix, file, scrape_dir, arrays)

        stages = (
            LayoutFile.STAGE,
            LayoutFile.NODES,
            LayoutFile.TREE,
            LayoutFile.EDGES,
            LayoutFile.SURFACES,
        )
        for name in stages:
            query = (
                PgQueryBuilder(schema=sql.Identifier(schema))
                .read(layout_dir / name)
                .build()
            )
            await ix.execute(query.text, query.params)

        return await apply_layout(ix, layout_dir, schema)


class RetryableError(Exception):
    """Попытка сорвалась по временной причине: каталог менялся, ix или источник
    заняты. Текст — причина для журнала."""


async def attempt_scrape(
    database: IxDatabase, source: ScrapeSource, package_dir: Path
) -> list[ApplyRow]:
    """Одна попытка с разбором отказов: временные уходят RetryableError, остальные
    сразу ScrapeWorkerError; сводка apply сверяется по planned и applied."""
    where = source.describe()
    try:
        rows = await scrape_once(database, source, package_dir)
    except CatalogChangedError as exc:
        raise RetryableError(f"catalog changed during read: {exc}") from exc
    except (LockNotAvailable, SerializationFailure) as exc:
        raise RetryableError(f"ix busy: {exc}".strip()) from exc
    except ScrapeSourceBusyError as exc:
        raise RetryableError(f"source busy: {exc}") from exc
    except PostgresError as exc:
        raise ScrapeWorkerError(
            f"scrape {where}: ix {database.postgres.where()}: {exc}"
        ) from exc
    except ScrapeSourceError as exc:
        raise ScrapeWorkerError(f"scrape {where}: source failed: {exc}") from exc
    except psycopg.Error as exc:
        raise ScrapeWorkerError(
            f"scrape {where}: ix {database.postgres.where()} failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    mismatched: list[str] = []
    for row in rows:
        if row.planned != row.applied:
            mismatched.append(row.op)

    if mismatched:
        raise ScrapeWorkerError(f"apply: planned <> applied for {mismatched}")

    return rows


async def scrape_source(
    database: IxDatabase,
    source: ScrapeSource,
    package_dir: Path,
    attempts: int,
) -> ScrapeReport:
    """Прогон одного источника с повторами; каждая попытка на свежих сессиях."""
    where = source.describe()
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            rows = await attempt_scrape(database, source, package_dir)
        except RetryableError as exc:
            last = str(exc)
            logger.warning("attempt %d/%d: %s", attempt, attempts, last)
            continue

        return ScrapeReport(source=where, rows=rows, attempts=attempt)

    raise ScrapeWorkerError(f"scrape {where}: {attempts} attempts failed, last: {last}")


class SourceConfigBase(BaseModel):
    """Один источник секции: имя для выбора из командной строки; профиль подключения
    добавляет реализация."""

    name: str = Field(min_length=1)


S = TypeVar("S", bound=SourceConfigBase)


class ScraperConfigBase(IxDatabase, Generic[S]):
    """Секция скрапера: база ix, список источников, число попыток и сколько
    источников снимать одновременно. Реализация задаёт модель источника и собирает
    по ней ScrapeSource."""

    sources: Sequence[S] = Field(min_length=1)
    attempts: int = Field(gt=0, default=3)
    parallel_sources: int = Field(ge=1, default=1)

    def find_source(self, name: str) -> S:
        for item in self.sources:
            if item.name == name:
                return item

        listed = ", ".join(item.name for item in self.sources)
        raise ScrapeWorkerError(f"source {name!r} is not among sources: {listed}")

    def select_sources(self, name: str) -> list[S]:
        """Все источники секции или один по имени."""
        if not name:
            return list(self.sources)

        return [self.find_source(name)]

    @abstractmethod
    def scrape_source(self, item: S) -> ScrapeSource:
        """Источник прогона по строке секции."""


def scrape_in_process(
    config: ScraperConfigBase[Any],
    source_name: str,
    package_dir: Path,
    krb: KerberosWorkspaceConfig | None,
) -> ScrapeReport:
    """Вход процесса источника: свой лог, свой каталог kerberos, свой event loop,
    один прогон."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if krb is not None:
        krb.apply()

    item = config.find_source(source_name)
    source = config.scrape_source(item)

    return asyncio.run(scrape_source(config, source, package_dir, config.attempts))


async def run_sources(
    config: ScraperConfigBase[Any],
    package_dir: Path,
    krb: KerberosWorkspaceConfig | None,
    source_name: str = "",
) -> list[ScrapeReport]:
    """Прогон по источникам: процесс на источник, parallel_sources процессов разом.
    Отчёты в порядке источников."""
    selected = config.select_sources(source_name)

    return await asyncio.to_thread(_run_processes, config, selected, package_dir, krb)


def _run_processes(
    config: ScraperConfigBase[Any],
    selected: Sequence[Any],
    package_dir: Path,
    krb: KerberosWorkspaceConfig | None,
) -> list[ScrapeReport]:
    """Ожидание итогов пула блокирует, поэтому идёт в потоке рядом с циклом."""
    reports: list[ScrapeReport] = []
    with ProcessPoolExecutor(
        max_workers=config.parallel_sources,
        mp_context=multiprocessing.get_context("spawn"),
        max_tasks_per_child=1,
    ) as pool:
        futures = []
        for item in selected:
            futures.append(
                pool.submit(scrape_in_process, config, item.name, package_dir, krb)
            )

        for item, future in zip(selected, futures, strict=True):
            try:
                reports.append(future.result())
            except ScrapeWorkerError:
                raise
            except Exception as exc:
                raise ScrapeWorkerError(
                    f"source {item.name}: the scrape process failed: {exc}"
                ) from exc

    return reports


class Command(StrEnum):
    UPGRADE = "upgrade"
    RUN = "run"


def parse_args(
    prog: str, description: str, section: str, argv: Sequence[str] | None
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument(
        "command",
        type=Command,
        choices=list(Command),
        help="upgrade — накатить схему пакета в базу ix; run — снять каталог.",
    )
    parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help=f"Файл конфига приложения (toml), секция [{section}].",
    )
    parser.add_argument(
        "--source",
        default="",
        help=f"Имя источника из [{section}].sources; пусто — все по порядку.",
    )

    return parser.parse_args(argv)


async def run_cli(
    prog: str,
    description: str,
    section: str,
    package_dir: Path,
    config_type: type[ScraperConfigBase[Any]],
) -> None:
    """Команда пакета-скрапера: upgrade накатывает schema/, run снимает источники."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    try:
        args = parse_args(prog, description, section, None)
        krb = enter_kerberos(args.config)
        if args.command is Command.UPGRADE:
            database = bind_section(args.config, section, IxDatabase)
            upgrade = SchemaUpgrade(package_dir / PackageDir.SCHEMA)
            report = await upgrade.run(database)
            logger.info("schema applied: %s", ", ".join(report.files))
            return

        config = bind_section(args.config, section, config_type)
        for report in await run_sources(config, package_dir, krb, args.source):
            logger.info("done: %s", report.line())
    except (ConfigError, SchemaUpgradeError, ScrapeWorkerError) as exc:
        raise SystemExit(str(exc)) from exc
