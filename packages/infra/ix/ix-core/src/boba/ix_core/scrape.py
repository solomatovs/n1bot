"""Общий цикл скрапера каталога: строки источника потоком в temp raw_* сессии ix,
сверка перечитыванием и except all на стороне ix, стадии layout/ в autocommit,
advisory-замок на scope, apply одной транзакцией repeatable read, повторы при
изменении каталога во время чтения и занятом ix.

Источник (PostgreSQL, ClickHouse) даёт реализацию ScrapeSource: открыть сессию,
сказать, подходит ли файл scrape/ серверу по заголовкам, отдать строки запроса
потоком и назвать свой адрес для raw_source. Всё остальное — файлы scrape/ и layout/
пакета и этот модуль. Пакет источника собирает команду из ScrapeApp, отдавая ей
модель своей секции конфига.

Ошибки:
ScrapeWorkerError — ix недоступен, контракт файлов нарушен, источник отказал,
    попытки исчерпаны (каталог менялся во время чтения или ix занят).
ScrapeSourceError — выпускает реализация источника: источник недоступен или
    отклонил запрос; ScrapeSourceBusyError — отказ временный, прогон повторяется.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
from abc import abstractmethod
from collections.abc import (
    AsyncIterator,
    Iterator,
    Mapping,
    Sequence,
)
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
    "ScrapeApp",
    "ScrapeFile",
    "ScrapeHeaders",
    "ScrapeSession",
    "ScrapeSource",
    "ScrapeSourceBusyError",
    "ScrapeSourceError",
    "ScrapeWorker",
    "ScrapeWorkerError",
    "ScraperConfigBase",
    "SourceAddressBase",
    "SourceConfigBase",
    "SourceRows",
]

logger = logging.getLogger("ix-scrape")


class ScrapeWorkerError(Exception):
    """Ошибка прогона скрапера."""


class ScrapeSourceError(Exception):
    """Источник недоступен или отклонил запрос; поднимает реализация источника."""


class ScrapeSourceBusyError(ScrapeSourceError):
    """Временный отказ источника (замок, сериализация): прогон повторяется."""


class CatalogChangedError(Exception):
    """Каталог источника изменился между чтением и сверкой."""


class Header(StrEnum):
    """Заголовки файла scrape/, которые читает общий цикл; ворота по версии сервера
    (@min, @max и подобные) читает источник."""

    NAME = "name"
    WAVE = "wave"
    PARAMS = "params"
    COLLECT = "collect"


class Marker(StrEnum):
    VERIFY = "-- @verify"
    RAW_SOURCE = "raw_source"
    RAW_PREFIX = "raw_"
    VERIFY_PREFIX = "verify_"


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


class PackageDir(StrEnum):
    SCHEMA = "schema"
    SCRAPE = "scrape"
    LAYOUT = "layout"


class ScrapeHeaders:
    """Заголовки `-- @имя значение` в начале sql-файла."""

    PATTERN = re.compile(r"^-- @(\w+)(?:\s+(.*))?$", re.M)

    @classmethod
    def of(cls, text: str) -> dict[str, str]:
        headers: dict[str, str] = {}
        for match in cls.PATTERN.finditer(text):
            value = match.group(2)
            if value is None:
                value = ""
            headers[match.group(1)] = value.strip()

        return headers


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

    @classmethod
    def parse(cls, path: Path) -> ScrapeFile:
        text = path.read_text(encoding="utf-8")
        headers = ScrapeHeaders.of(text)
        if Header.NAME not in headers:
            raise ScrapeWorkerError(f"{path}: expected @name header")

        fetch_sql, _, verify_sql = text.partition(Marker.VERIFY)
        if not verify_sql.strip():
            raise ScrapeWorkerError(f"{path}: expected a {Marker.VERIFY} section")

        collect = headers.get(Header.COLLECT, "").split()
        collect_name = ""
        collect_column = ""
        if collect:
            collect_name = collect[0]
        if len(collect) > 1:
            collect_column = collect[1]

        return cls(
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

    @classmethod
    def all_under(cls, package_dir: Path) -> list[ScrapeFile]:
        files: list[ScrapeFile] = []
        for path in sorted((package_dir / PackageDir.SCRAPE).glob("*.sql")):
            files.append(cls.parse(path))

        return files


class ApplyRow(BaseModel):
    op: str
    planned: int
    applied: int


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
    def rows(
        self, name: str, query: str, params: Mapping[str, Sequence[object]]
    ) -> AbstractAsyncContextManager[SourceRows]:
        """Строки запроса потоком под именем name (курсор, журнал); массивы
        параметров по именам @params."""


class ScrapeSource(Protocol):
    """Источник каталога: адрес для raw_source и сессия на попытку."""

    @property
    @abstractmethod
    def address(self) -> SourceAddressBase: ...

    @abstractmethod
    def where(self) -> str:
        """Подпись источника для сообщений об ошибках."""

    @abstractmethod
    def session(self) -> AbstractAsyncContextManager[ScrapeSession]: ...


class Params:
    """Параметры scrape-файла: словарь массивов по именам из @params."""

    @staticmethod
    def of(
        arrays: Mapping[str, Sequence[object]], names: Sequence[str]
    ) -> dict[str, list[object]]:
        values: dict[str, list[object]] = {}
        for name in names:
            values[name] = list(arrays.get(name, ()))

        return values


class Pipeline:
    """Одна попытка прогона: строки источника потоком уходят в raw_* сессии ix,
    сверка перечитыванием через временную таблицу и except all на стороне ix, затем
    стадии и apply. В памяти Python только текущая строка и собранные массивы для
    параметров следующих волн."""

    def __init__(
        self,
        database: IxDatabase,
        source: ScrapeSource,
        files: Sequence[ScrapeFile],
        layout_dir: Path,
    ) -> None:
        self._database = database
        self._source = source
        self._files = files
        self._dir = layout_dir

    async def run(self) -> Sequence[ApplyRow]:
        async with (
            IxPool.session(self._database) as ix,
            self._source.session() as session,
        ):
            chosen = list(self._choose(session))

            await ix.execute(self._read(LayoutFile.RAW_SCHEMA))
            await self._register_source(ix)

            arrays: dict[str, Sequence[object]] = {}
            for file in chosen:
                arrays.update(await self._stream(session, ix, file, arrays))

            for file in chosen:
                await self._verify(session, ix, file, arrays)

            for name in (
                LayoutFile.STAGE,
                LayoutFile.NODES,
                LayoutFile.TREE,
                LayoutFile.EDGES,
                LayoutFile.SURFACES,
            ):
                await ix.execute(self._read(name))

            return await self._apply(ix)

    async def _register_source(self, ix: psycopg.AsyncConnection[Any]) -> None:
        columns = self._source.address.columns()
        names = sql.SQL(", ").join(sql.Identifier(name) for name in columns)
        marks = sql.SQL(", ").join(sql.Placeholder() for _ in columns)
        await ix.execute(
            sql.SQL("insert into {} ({}) values ({})").format(
                sql.Identifier(Marker.RAW_SOURCE), names, marks
            ),
            list(columns.values()),
        )

    def _choose(self, session: ScrapeSession) -> Iterator[ScrapeFile]:
        by_name: dict[str, list[ScrapeFile]] = {}
        for file in self._files:
            by_name.setdefault(file.name, []).append(file)

        for name in sorted(by_name, key=lambda n: (by_name[n][0].wave, n)):
            variants = [f for f in by_name[name] if session.applies(f.headers)]
            if len(variants) > 1:
                listed = ", ".join(f.path.name for f in variants)
                raise ScrapeWorkerError(
                    f"scrape {name}: {len(variants)} variants apply to "
                    f"{self._source.where()}: {listed}"
                )

            if variants:
                yield variants[0]

    async def _stream(
        self,
        session: ScrapeSession,
        ix: psycopg.AsyncConnection[Any],
        file: ScrapeFile,
        arrays: Mapping[str, Sequence[object]],
    ) -> dict[str, Sequence[object]]:
        """Выборка одного файла: COPY в raw_<name> по колонкам результата, попутно
        массив @collect."""
        values = Params.of(arrays, file.params)
        collected: list[object] = []
        count = 0
        async with session.rows(file.name, file.fetch_sql, values) as rows:
            columns = list(rows.columns)
            position = -1
            if file.collect:
                position = columns.index(file.collect_column)

            target = sql.Identifier(f"{Marker.RAW_PREFIX}{file.name}")
            async with ix.cursor().copy(self._copy_of(target, columns)) as copy:
                async for row in rows:
                    await copy.write_row(row)
                    count += 1
                    if position >= 0:
                        collected.append(row[position])

        logger.info("scrape %s (%s): %d rows", file.name, file.path.name, count)

        if not file.collect:
            return {}

        return {file.collect: collected}

    async def _verify(
        self,
        session: ScrapeSession,
        ix: psycopg.AsyncConnection[Any],
        file: ScrapeFile,
        arrays: Mapping[str, Sequence[object]],
    ) -> None:
        """Сверка: строки @verify потоком в verify_<name>, затем сравнение с теми же
        колонками raw_<name> на стороне ix."""
        values = Params.of(arrays, file.params)
        raw = sql.Identifier(f"{Marker.RAW_PREFIX}{file.name}")
        check = sql.Identifier(f"{Marker.VERIFY_PREFIX}{file.name}")
        async with session.rows(
            f"{Marker.VERIFY_PREFIX}{file.name}", file.verify_sql, values
        ) as rows:
            columns = list(rows.columns)
            keys = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
            await ix.execute(
                sql.SQL("create temp table {} as select {} from {} where false").format(
                    check, keys, raw
                )
            )
            async with ix.cursor().copy(self._copy_of(check, columns)) as copy:
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

    @staticmethod
    def _copy_of(target: sql.Identifier, columns: Sequence[str]) -> sql.Composed:
        names = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
        return sql.SQL("copy {} ({}) from stdin").format(target, names)

    async def _apply(self, ix: psycopg.AsyncConnection[Any]) -> Sequence[ApplyRow]:
        try:
            await ix.execute(self._read(LayoutFile.LOCK))
            await ix.execute("begin isolation level repeatable read")
            try:
                cur = await ix.execute(self._read(LayoutFile.APPLY))
                rows = await self._last_result(cur)
                await ix.execute("commit")
            except Exception:
                await ix.execute("rollback")
                raise
        finally:
            await ix.execute(self._read(LayoutFile.UNLOCK))

        summary: list[ApplyRow] = []
        for row in rows:
            summary.append(
                ApplyRow.model_validate(
                    {"op": row[0], "planned": row[1], "applied": row[2]}
                )
            )

        return summary

    @staticmethod
    async def _last_result(
        cur: psycopg.AsyncCursor[Any],
    ) -> list[tuple[object, ...]]:
        """Скрипт из многих statement'ов: сводка это последний набор строк."""
        rows: list[tuple[object, ...]] = []
        while True:
            if cur.description is not None:
                rows = [tuple(r) for r in await cur.fetchall()]
            if not cur.nextset():
                return rows

    def _read(self, name: LayoutFile) -> sql.Composed:
        text = (self._dir / name).read_text(encoding="utf-8")
        return SchemaName.render(text, self._database.db_schema)


class ScrapeWorker:
    """Полный прогон одного источника с повторами. Каждая попытка открывает обе сессии
    заново: временные raw_* предыдущей попытки исчезают вместе с сессией."""

    def __init__(
        self,
        database: IxDatabase,
        source: ScrapeSource,
        package_dir: Path,
        attempts: int,
    ) -> None:
        self._database = database
        self._source = source
        self._files = ScrapeFile.all_under(package_dir)
        self._layout_dir = package_dir / PackageDir.LAYOUT
        self._attempts = attempts

    async def run(self) -> Sequence[ApplyRow]:
        last = ""
        for attempt in range(1, self._attempts + 1):
            pipeline = Pipeline(
                self._database, self._source, self._files, self._layout_dir
            )
            try:
                summary = await pipeline.run()
            except CatalogChangedError as exc:
                last = f"catalog changed during read: {exc}"
            except (LockNotAvailable, SerializationFailure) as exc:
                last = f"ix busy: {exc}".strip()
            except ScrapeSourceBusyError as exc:
                last = f"source busy: {exc}"
            except IxDatabaseError as exc:
                raise ScrapeWorkerError(
                    f"scrape {self._source.where()}: {exc}"
                ) from exc
            except ScrapeSourceError as exc:
                raise ScrapeWorkerError(
                    f"scrape {self._source.where()}: source failed: {exc}"
                ) from exc
            except psycopg.Error as exc:
                ix = self._database.postgres.where()
                raise ScrapeWorkerError(
                    f"scrape {self._source.where()}: ix {ix} failed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            else:
                mismatched = [r.op for r in summary if r.planned != r.applied]
                if mismatched:
                    raise ScrapeWorkerError(
                        f"apply: planned <> applied for {mismatched}"
                    )
                return summary

            logger.warning("attempt %d/%d: %s", attempt, self._attempts, last)

        raise ScrapeWorkerError(
            f"scrape {self._source.where()}: {self._attempts} attempts failed, "
            f"last: {last}"
        )


class SourceConfigBase(BaseModel):
    """Один источник секции: имя для выбора из командной строки; профиль подключения
    добавляет реализация."""

    name: str = Field(min_length=1)


S = TypeVar("S", bound=SourceConfigBase)


class ScraperConfigBase(IxDatabase, Generic[S]):
    """Секция скрапера: база ix, список источников и число попыток. Реализация
    задаёт модель источника и собирает по ней ScrapeSource."""

    sources: Sequence[S] = Field(min_length=1)
    attempts: int = Field(gt=0, default=3)

    def source(self, name: str) -> S:
        for item in self.sources:
            if item.name == name:
                return item

        listed = ", ".join(item.name for item in self.sources)
        raise ScrapeWorkerError(f"source {name!r} is not among sources: {listed}")

    def selected(self, name: str) -> Sequence[S]:
        """Все источники секции или один по имени."""
        if not name:
            return list(self.sources)

        return [self.source(name)]

    @abstractmethod
    def scrape_source(self, item: S) -> ScrapeSource:
        """Источник прогона по строке секции."""


class Command(StrEnum):
    """Что делает запуск: накатить свою схему или снять источники."""

    UPGRADE = "upgrade"
    RUN = "run"


class ScrapeApp(Generic[S]):
    """Команда пакета-скрапера: `upgrade` накатывает schema/ пакета, `run` снимает
    источники секции по порядку или один по --source."""

    def __init__(
        self,
        prog: str,
        description: str,
        section: str,
        package_dir: Path,
        config: type[ScraperConfigBase[S]],
    ) -> None:
        self._prog = prog
        self._description = description
        self._section = section
        self._package_dir = package_dir
        self._config = config

    def main(self, argv: Sequence[str] | None = None) -> None:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(name)s %(message)s"
        )

        try:
            command, config_path, selected = self._parse(argv)

            if command is Command.UPGRADE:
                database = bind_section(config_path, self._section, IxDatabase)
                upgrade = SchemaUpgrade(self._package_dir / PackageDir.SCHEMA)
                report = asyncio.run(upgrade.run(database))
                logger.info("schema applied: %s", ", ".join(report.files))
                return

            config = bind_section(config_path, self._section, self._config)
            asyncio.run(self.run_sources(config, config.selected(selected)))
        except (ConfigError, SchemaUpgradeError, ScrapeWorkerError) as exc:
            raise SystemExit(str(exc)) from exc

    async def run_sources(
        self, config: ScraperConfigBase[S], sources: Sequence[S]
    ) -> None:
        """Снять перечисленные источники по порядку, итог каждого в журнал."""
        for item in sources:
            logger.info("source %s: scrape started", item.name)
            worker = ScrapeWorker(
                config, config.scrape_source(item), self._package_dir, config.attempts
            )
            summary = await worker.run()
            for row in summary:
                logger.info(
                    "source %s: %s planned=%d applied=%d",
                    item.name,
                    row.op,
                    row.planned,
                    row.applied,
                )

    def _parse(self, argv: Sequence[str] | None) -> tuple[Command, Path, str]:
        parser = argparse.ArgumentParser(prog=self._prog, description=self._description)
        parser.add_argument(
            "command",
            type=Command,
            choices=list(Command),
            help=(
                "upgrade — накатить схему пакета в базу ix (идемпотентно, ядро "
                "должно быть уже накачено пакетом ix-core); run — снять каталог."
            ),
        )
        parser.add_argument(
            "--config",
            required=True,
            type=Path,
            help=(
                "Путь к файлу конфига приложения (toml). База ix, источники и "
                f"границы прогона берутся из секции [{self._section}]."
            ),
        )
        parser.add_argument(
            "--source",
            default="",
            help=(
                f"Имя источника из [{self._section}].sources. Пусто — снять все "
                "источники секции по порядку."
            ),
        )
        args = parser.parse_args(argv)

        return args.command, args.config, args.source
