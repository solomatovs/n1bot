"""Общий цикл скраперов каталога: процесс на источник, внутри всё по порядку.

Родитель читает конфиг и раздаёт источники процессам: по одному источнику на
процесс, процесс умирает вместе с прогоном. Внутри одна попытка это одно выделенное
соединение к ix и одна сессия источника: файлы scrape/ по волнам, строки каждого
потоком в temp raw_<name> через COPY, сверка перечитыванием и except all на стороне
ix, стадии layout/ в autocommit, advisory-замок на scope, apply одной транзакцией
repeatable read. Каталог изменился во время чтения или ix занят — попытка
повторяется с новыми сессиями, temp-таблицы прежней умирают вместе с ней.

Источник (PostgreSQL, ClickHouse, Oracle) даёт реализацию ScrapeSource: открыть
сессию, сказать, подходит ли файл серверу по заголовкам, отдать строки запроса
потоком и назвать свой адрес для raw_source. Единственное накопление в памяти —
массивы @collect: колонка результата одной волны идёт параметром запросов следующих.
Ими пользуется pg-скрапер; Oracle и ClickHouse задают границы подзапросом в SQL.

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
import re
import resource
from abc import abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import AbstractAsyncContextManager
from enum import StrEnum
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

import psycopg
from psycopg import sql
from psycopg.errors import LockNotAvailable, SerializationFailure
from pydantic import BaseModel, ConfigDict, Field

from boba.config import ConfigError, bind_section
from boba.ix_core.database import IxDatabase, IxDatabaseError, IxPool
from boba.ix_core.schema_name import SchemaName
from boba.ix_core.upgrade import SchemaUpgrade, SchemaUpgradeError

__all__ = [
    "ApplyRow",
    "CatalogChangedError",
    "Header",
    "LayoutFile",
    "ScrapeFile",
    "ScrapeReport",
    "ScrapeSession",
    "ScrapeSource",
    "ScrapeSourceBusyError",
    "ScrapeSourceError",
    "ScrapeWorkerError",
    "ScraperConfigBase",
    "SourceAddressBase",
    "SourceConfigBase",
    "SourceRows",
    "StreamRows",
    "load_scrape_files",
    "parse_headers",
    "parse_version",
    "render_version",
    "run_cli",
    "run_sources",
    "scrape_in_process",
    "scrape_source",
    "version_applies",
]

logger = logging.getLogger("ix-scrape")

VERIFY_MARKER = "-- @verify"
RAW_SOURCE_TABLE = "raw_source"
RAW_PREFIX = "raw_"
VERIFY_PREFIX = "verify_"
SCHEMA_DIR = "schema"
SCRAPE_DIR = "scrape"
LAYOUT_DIR = "layout"
HEADER_PATTERN = re.compile(r"^-- @(\w+)(?:\s+(.*))?$", re.M)
LOG_FORMAT = "%(asctime)s %(name)s %(message)s"
VERSION_FLOOR = (0,)
VERSION_CEILING = (999999,)


class ScrapeWorkerError(Exception):
    """Ошибка прогона скрапера."""


class ScrapeSourceError(Exception):
    """Источник недоступен или отклонил запрос; поднимает реализация источника."""


class ScrapeSourceBusyError(ScrapeSourceError):
    """Временный отказ источника (замок, сериализация): попытка повторяется."""


class CatalogChangedError(Exception):
    """Каталог источника изменился между чтением и сверкой."""


class Header(StrEnum):
    """Заголовки `-- @имя значение` файла scrape/."""

    NAME = "name"
    WAVE = "wave"
    PARAMS = "params"
    COLLECT = "collect"
    MIN = "min"
    MAX = "max"


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


STAGE_FILES = (
    LayoutFile.STAGE,
    LayoutFile.NODES,
    LayoutFile.TREE,
    LayoutFile.EDGES,
    LayoutFile.SURFACES,
)


class ScrapeFile(BaseModel):
    """Файл scrape/: запрос выборки, запрос сверки и заголовки. Заголовки остаются в
    тексте запроса: для сервера это обычные комментарии."""

    model_config = ConfigDict(frozen=True)

    path: Path
    name: str
    wave: int
    params: Sequence[str] = ()
    collect: str = ""
    collect_column: str = ""
    fetch_sql: str
    verify_sql: str
    headers: Mapping[str, str]


class ApplyRow(BaseModel):
    op: str
    planned: int
    applied: int


class ScrapeReport(BaseModel):
    """Итог прогона одного источника: сводка apply, попытки, пик RSS процесса."""

    source: str
    rows: Sequence[ApplyRow]
    attempts: int
    peak_rss_mib: int

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

        return (
            f"{self.source}: {changed}, attempts={self.attempts} "
            f"rss={self.peak_rss_mib}MiB"
        )


class SourceAddressBase(BaseModel):
    """Адрес источника для raw_source: поля модели это колонки таблицы. Реализация
    добавляет свои (у PostgreSQL база), совпадающие с raw_source её layout."""

    model_config = ConfigDict(frozen=True)

    scheme: str
    host: str
    port: int

    def columns(self) -> dict[str, object]:
        return self.model_dump()


class SourceRows(Protocol):
    """Результат одного запроса к источнику: имена колонок и строки потоком."""

    @property
    @abstractmethod
    def columns(self) -> Sequence[str]: ...

    @abstractmethod
    def __aiter__(self) -> AsyncIterator[Sequence[object]]: ...


class ScrapeSession(Protocol):
    """Открытая сессия источника на одну попытку прогона."""

    @abstractmethod
    def applies(self, headers: Mapping[str, str]) -> bool:
        """Подходит ли файл с такими заголовками серверу этой сессии."""

    @abstractmethod
    def fetch_rows(
        self, name: str, query: str, params: Mapping[str, Sequence[object]]
    ) -> AbstractAsyncContextManager[SourceRows]:
        """Строки запроса потоком под именем name; массивы по именам @params."""


class ScrapeSource(Protocol):
    """Источник каталога: адрес для raw_source и сессия на попытку."""

    @property
    @abstractmethod
    def address(self) -> SourceAddressBase: ...

    @abstractmethod
    def describe(self) -> str:
        """Подпись источника для сообщений об ошибках."""

    @abstractmethod
    def open_session(self) -> AbstractAsyncContextManager[ScrapeSession]: ...


class StreamRows(SourceRows):
    """Строки потока драйвера; отказ сервера по дороге уходит ScrapeSourceError."""

    def __init__(
        self,
        columns: Sequence[str],
        blocks: AsyncIterator[Sequence[object]],
        name: str,
        where: str,
        errors: tuple[type[Exception], ...],
    ) -> None:
        self._columns = tuple(columns)
        self._blocks = blocks
        self._name = name
        self._where = where
        self._errors = errors

    @property
    def columns(self) -> Sequence[str]:
        return self._columns

    async def __aiter__(self) -> AsyncIterator[Sequence[object]]:
        try:
            async for row in self._blocks:
                yield row
        except self._errors as exc:
            raise ScrapeSourceError(
                f"reading {self._name} from {self._where}: {exc}"
            ) from exc


def parse_headers(text: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for match in HEADER_PATTERN.finditer(text):
        value = match.group(2)
        if value is None:
            value = ""

        headers[match.group(1)] = value.strip()

    return headers


def parse_scrape_file(path: Path) -> ScrapeFile:
    text = path.read_text(encoding="utf-8")
    headers = parse_headers(text)
    if Header.NAME not in headers:
        raise ScrapeWorkerError(f"{path}: expected @name header")

    fetch_sql, _, verify_sql = text.partition(VERIFY_MARKER)
    if not verify_sql.strip():
        raise ScrapeWorkerError(f"{path}: expected a {VERIFY_MARKER} section")

    collect = headers.get(Header.COLLECT, "").split()
    collect_name = ""
    collect_column = ""
    if collect:
        collect_name = collect[0]

    if len(collect) > 1:
        collect_column = collect[1]

    return ScrapeFile(
        path=path,
        name=headers[Header.NAME],
        wave=int(headers.get(Header.WAVE, "1")),
        params=headers.get(Header.PARAMS, "").split(),
        collect=collect_name,
        collect_column=collect_column,
        fetch_sql=fetch_sql.strip(),
        verify_sql=verify_sql.strip(),
        headers=headers,
    )


def load_scrape_files(package_dir: Path) -> list[ScrapeFile]:
    files: list[ScrapeFile] = []
    for path in sorted((package_dir / SCRAPE_DIR).glob("*.sql")):
        files.append(parse_scrape_file(path))

    return files


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


def render_version(parts: Sequence[int]) -> str:
    return ".".join(str(part) for part in parts)


def version_applies(headers: Mapping[str, str], server: Sequence[int]) -> bool:
    """Ворота @min и @max сравниваются по своей длине: @max 19 отсекает 21.0.0, но
    пропускает 19.3."""
    low = VERSION_FLOOR
    if raw := headers.get(Header.MIN):
        low = parse_version(raw)

    high = VERSION_CEILING
    if raw := headers.get(Header.MAX):
        high = parse_version(raw)

    if tuple(server[: len(low)]) < low:
        return False

    return tuple(server[: len(high)]) <= high


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
            if session.applies(file.headers):
                variants.append(file)

        if len(variants) > 1:
            listed = ", ".join(file.path.name for file in variants)
            raise ScrapeWorkerError(
                f"scrape {name}: {len(variants)} variants apply to {where}: {listed}"
            )

        if variants:
            chosen.append(variants[0])

    return chosen


def read_layout(layout_dir: Path, name: LayoutFile, db_schema: str) -> sql.Composed:
    text = (layout_dir / name).read_text(encoding="utf-8")

    return SchemaName.render(text, db_schema)


def compose_copy(target: sql.Identifier, columns: Sequence[str]) -> sql.Composed:
    names = sql.SQL(", ").join(sql.Identifier(column) for column in columns)

    return sql.SQL("copy {} ({}) from stdin").format(target, names)


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
    names = sql.SQL(", ").join(sql.Identifier(name) for name in columns)
    marks = sql.SQL(", ").join(sql.Placeholder() for _ in columns)
    await ix.execute(
        sql.SQL("insert into {} ({}) values ({})").format(
            sql.Identifier(RAW_SOURCE_TABLE), names, marks
        ),
        list(columns.values()),
    )


async def copy_rows(
    session: ScrapeSession,
    ix: psycopg.AsyncConnection[Any],
    file: ScrapeFile,
    arrays: Mapping[str, Sequence[object]],
) -> dict[str, Sequence[object]]:
    """Выборка одного файла потоком в raw_<name>; попутно колонка @collect."""
    values = pick_params(arrays, file.params)
    collected: list[object] = []
    count = 0
    async with session.fetch_rows(file.name, file.fetch_sql, values) as rows:
        columns = list(rows.columns)
        position = -1
        if file.collect:
            position = columns.index(file.collect_column)

        target = sql.Identifier(f"{RAW_PREFIX}{file.name}")
        async with ix.cursor().copy(compose_copy(target, columns)) as copy:
            async for row in rows:
                await copy.write_row(row)
                count += 1
                if position >= 0:
                    collected.append(row[position])

    logger.info("scrape %s (%s): %d rows", file.name, file.path.name, count)

    if not file.collect:
        return {}

    return {file.collect: collected}


async def verify_rows(
    session: ScrapeSession,
    ix: psycopg.AsyncConnection[Any],
    file: ScrapeFile,
    arrays: Mapping[str, Sequence[object]],
) -> None:
    """Сверка: строки @verify потоком в verify_<name>, затем except all с raw_<name>
    в обе стороны на стороне ix."""
    values = pick_params(arrays, file.params)
    raw = sql.Identifier(f"{RAW_PREFIX}{file.name}")
    check = sql.Identifier(f"{VERIFY_PREFIX}{file.name}")
    async with session.fetch_rows(
        f"{VERIFY_PREFIX}{file.name}", file.verify_sql, values
    ) as rows:
        columns = list(rows.columns)
        keys = sql.SQL(", ").join(sql.Identifier(column) for column in columns)
        await ix.execute(
            sql.SQL("create temp table {} as select {} from {} where false").format(
                check, keys, raw
            )
        )
        async with ix.cursor().copy(compose_copy(check, columns)) as copy:
            async for row in rows:
                await copy.write_row(row)

    diff_cur = await ix.execute(
        sql.SQL(
            "select count(*) from ((select {k} from {r} except all select {k} "
            "from {c}) union all (select {k} from {c} except all select {k} from "
            "{r})) d"
        ).format(k=keys, r=raw, c=check)
    )
    diff = await diff_cur.fetchone()
    await ix.execute(sql.SQL("drop table {}").format(check))

    if diff is None:
        raise ScrapeWorkerError(f"verify {file.name}: expected a count, got none")

    if int(diff[0]) != 0:
        raise CatalogChangedError(file.name)


async def read_last_rows(cur: psycopg.AsyncCursor[Any]) -> list[tuple[object, ...]]:
    """Скрипт из многих statement'ов: сводка это последний набор строк."""
    rows: list[tuple[object, ...]] = []
    while True:
        if cur.description is not None:
            rows = [tuple(row) for row in await cur.fetchall()]

        if not cur.nextset():
            return rows


async def apply_layout(
    ix: psycopg.AsyncConnection[Any], layout_dir: Path, db_schema: str
) -> list[ApplyRow]:
    """Замок на scope, apply одной транзакцией repeatable read, замок снят."""
    try:
        await ix.execute(read_layout(layout_dir, LayoutFile.LOCK, db_schema))
        await ix.execute("begin isolation level repeatable read")
        try:
            cur = await ix.execute(read_layout(layout_dir, LayoutFile.APPLY, db_schema))
            rows = await read_last_rows(cur)
            await ix.execute("commit")
        except Exception:
            await ix.execute("rollback")
            raise
    finally:
        await ix.execute(read_layout(layout_dir, LayoutFile.UNLOCK, db_schema))

    summary: list[ApplyRow] = []
    for row in rows:
        summary.append(
            ApplyRow.model_validate(
                {"op": row[0], "planned": row[1], "applied": row[2]}
            )
        )

    return summary


async def scrape_once(
    database: IxDatabase,
    source: ScrapeSource,
    files: Sequence[ScrapeFile],
    layout_dir: Path,
) -> list[ApplyRow]:
    """Одна попытка: raw-таблицы, строки всех файлов, сверка, стадии, apply."""
    schema = database.db_schema
    async with IxPool.dedicated(database) as ix, source.open_session() as session:
        chosen = choose_files(files, session, source.describe())

        await ix.execute(read_layout(layout_dir, LayoutFile.RAW_SCHEMA, schema))
        await register_source(ix, source.address)

        arrays: dict[str, Sequence[object]] = {}
        for file in chosen:
            arrays.update(await copy_rows(session, ix, file, arrays))

        for file in chosen:
            await verify_rows(session, ix, file, arrays)

        for name in STAGE_FILES:
            await ix.execute(read_layout(layout_dir, name, schema))

        return await apply_layout(ix, layout_dir, schema)


class RetryableError(Exception):
    """Попытка сорвалась по временной причине: каталог менялся, ix или источник
    заняты. Текст — причина для журнала."""


async def attempt_scrape(
    database: IxDatabase,
    source: ScrapeSource,
    files: Sequence[ScrapeFile],
    layout_dir: Path,
) -> list[ApplyRow]:
    """Одна попытка с разбором отказов: временные уходят RetryableError, остальные
    сразу ScrapeWorkerError; сводка apply сверяется по planned и applied."""
    where = source.describe()
    try:
        rows = await scrape_once(database, source, files, layout_dir)
    except CatalogChangedError as exc:
        raise RetryableError(f"catalog changed during read: {exc}") from exc
    except (LockNotAvailable, SerializationFailure) as exc:
        raise RetryableError(f"ix busy: {exc}".strip()) from exc
    except ScrapeSourceBusyError as exc:
        raise RetryableError(f"source busy: {exc}") from exc
    except IxDatabaseError as exc:
        raise ScrapeWorkerError(f"scrape {where}: {exc}") from exc
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
    files = load_scrape_files(package_dir)
    layout_dir = package_dir / LAYOUT_DIR
    where = source.describe()
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            rows = await attempt_scrape(database, source, files, layout_dir)
        except RetryableError as exc:
            last = str(exc)
            logger.warning("attempt %d/%d: %s", attempt, attempts, last)
            continue

        return ScrapeReport(
            source=where, rows=rows, attempts=attempt, peak_rss_mib=peak_rss_mib()
        )

    raise ScrapeWorkerError(f"scrape {where}: {attempts} attempts failed, last: {last}")


def peak_rss_mib() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss >> 10


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
    config: ScraperConfigBase[Any], source_name: str, package_dir: Path
) -> ScrapeReport:
    """Вход процесса источника: свой лог, свой event loop, один прогон."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    item = config.find_source(source_name)
    source = config.scrape_source(item)

    return asyncio.run(scrape_source(config, source, package_dir, config.attempts))


def run_sources(
    config: ScraperConfigBase[Any], package_dir: Path, source_name: str = ""
) -> list[ScrapeReport]:
    """Прогон по источникам: процесс на источник, parallel_sources процессов разом.
    Отчёты в порядке источников."""
    selected = config.select_sources(source_name)
    reports: list[ScrapeReport] = []
    with ProcessPoolExecutor(
        max_workers=config.parallel_sources,
        mp_context=multiprocessing.get_context("spawn"),
        max_tasks_per_child=1,
    ) as pool:
        futures = []
        for item in selected:
            futures.append(
                pool.submit(scrape_in_process, config, item.name, package_dir)
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


def run_cli(
    prog: str,
    description: str,
    section: str,
    package_dir: Path,
    config_type: type[ScraperConfigBase[Any]],
) -> None:
    """Команда пакета-скрапера: upgrade накатывает schema/, run снимает источники."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

    try:
        args = parse_args(prog, description, section, None)
        if args.command is Command.UPGRADE:
            database = bind_section(args.config, section, IxDatabase)
            upgrade = SchemaUpgrade(package_dir / SCHEMA_DIR)
            report = asyncio.run(upgrade.run(database))
            logger.info("schema applied: %s", ", ".join(report.files))
            return

        config = bind_section(args.config, section, config_type)
        for report in run_sources(config, package_dir, args.source):
            logger.info("done: %s", report.line())
    except (ConfigError, SchemaUpgradeError, ScrapeWorkerError) as exc:
        raise SystemExit(str(exc)) from exc
