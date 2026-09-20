"""Воркер скрапера: снять каталог источника по файлам scrape/, сверить по xmin,
разложить в ix по файлам layout/. Всё по README пакета, шаги 1–8. Потоково: строки
источника идут в raw_* сессии ix через COPY по одной, в памяти только массивы OID.

Ошибки:
ScrapeWorkerError — источник или ix недоступны, контракт файлов нарушен, три попытки
    не дали согласованного результата (каталог менялся во время чтения или ix занят).
"""

from __future__ import annotations

import argparse
import logging
import re
from collections.abc import Iterator, Sequence
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.errors import LockNotAvailable, SerializationFailure
from pydantic import BaseModel, Field

from boba.config import ConfigError, bind_section
from boba.pg_ix_core.schema_name import SchemaName, StorageSchema
from boba.pg_ix_core.upgrade import SchemaUpgrade, SchemaUpgradeError, UpgradeConfig

logger = logging.getLogger("pg-meta-scraper")


class ScrapeWorkerError(Exception):
    """Ошибка прогона скрапера."""


class Header(StrEnum):
    NAME = "name"
    WAVE = "wave"
    PARAMS = "params"
    KEY = "key"
    COLLECT = "collect"
    MIN = "min"
    MAX = "max"
    ONLY = "only"
    NOT = "not"


class Marker(StrEnum):
    VERIFY = "-- @verify"
    GP = "gp"
    GREENPLUM = "Greenplum"


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


class SourceConfig(BaseModel):
    """Источник снятия: имя для выбора из командной строки и строка подключения."""

    name: str = Field(min_length=1)
    dsn: str = Field(min_length=1)


class ScraperConfig(StorageSchema):
    """Секция [ix.meta_scraper]: база ix, список источников и границы прогона."""

    dsn: str = Field(min_length=1)
    sources: Sequence[SourceConfig] = Field(min_length=1)
    attempts: int = Field(gt=0, default=3)
    lock_timeout: str = "2s"
    statement_timeout: str = "30s"

    def source(self, name: str) -> SourceConfig:
        for item in self.sources:
            if item.name == name:
                return item

        listed = ", ".join(item.name for item in self.sources)
        msg = f"source {name!r} is not in [ix.meta_scraper].sources: {listed}"
        raise ScrapeWorkerError(msg)

    def run_of(self, source: SourceConfig) -> WorkerConfig:
        """Настройки одного прогона: выбранный источник плюс общие границы."""
        return WorkerConfig(
            source_dsn=source.dsn,
            ix_dsn=self.dsn,
            db_schema=self.db_schema,
            attempts=self.attempts,
            lock_timeout=self.lock_timeout,
            statement_timeout=self.statement_timeout,
        )


class WorkerConfig(StorageSchema):
    """Один прогон: откуда снимаем, куда раскладываем, сколько раз повторять."""

    source_dsn: str
    ix_dsn: str
    attempts: int = Field(gt=0, default=3)
    lock_timeout: str = "2s"
    statement_timeout: str = "30s"


class SourceAddress(BaseModel):
    scheme: str = "postgresql"
    host: str
    port: int
    database: str

    @classmethod
    def of_dsn(cls, dsn: str) -> SourceAddress:
        parts = conninfo_to_dict(dsn)
        host = parts.get("host")
        database = parts.get("dbname")
        if host is None or database is None:
            raise ScrapeWorkerError(
                f"source dsn: expected host and dbname, got {sorted(parts)}"
            )
        return cls(
            host=str(host),
            port=int(str(parts.get("port", 5432))),
            database=str(database),
        )


class ServerInfo(BaseModel):
    version_num: int
    is_greenplum: bool


class VersionGate(BaseModel):
    """Ворота файла по серверу: заголовки @min, @max, @only gp, @not gp. Базовый класс
    для файлов scrape и для DDL стенда в тестах."""

    min_version: int = 0
    max_version: int = 999999999
    only_gp: bool = False
    not_gp: bool = False

    @staticmethod
    def headers_of(text: str) -> dict[str, str]:
        headers: dict[str, str] = {}
        for match in re.finditer(r"^-- @(\w+)(?:\s+(.*))?$", text, re.M):
            headers[match.group(1)] = (match.group(2) or "").strip()
        return headers

    @classmethod
    def gate_of(cls, headers: dict[str, str]) -> VersionGate:
        return VersionGate(
            min_version=int(headers.get(Header.MIN, "0")),
            max_version=int(headers.get(Header.MAX, "999999999")),
            only_gp=headers.get(Header.ONLY) == Marker.GP,
            not_gp=headers.get(Header.NOT) == Marker.GP,
        )

    def applies(self, server: ServerInfo) -> bool:
        if (
            server.version_num < self.min_version
            or server.version_num > self.max_version
        ):
            return False
        if self.only_gp and not server.is_greenplum:
            return False
        return not (self.not_gp and server.is_greenplum)


class ScrapeFile(VersionGate):
    path: Path
    name: str
    wave: int
    params: Sequence[str] = ()
    key: Sequence[str]
    collect: str = ""
    collect_column: str = ""
    fetch_sql: str
    verify_sql: str

    @classmethod
    def parse(cls, path: Path) -> ScrapeFile:
        text = path.read_text(encoding="utf-8")
        headers = cls.headers_of(text)
        if Header.NAME not in headers or Header.KEY not in headers:
            raise ScrapeWorkerError(f"{path}: expected @name and @key headers")
        # заголовки остаются в тексте: для сервера это обычные комментарии
        fetch_sql, _, verify_sql = text.partition(Marker.VERIFY)
        collect = headers.get(Header.COLLECT, "").split()
        gate = cls.gate_of(headers)
        return cls(
            path=path,
            name=headers[Header.NAME],
            wave=int(headers.get(Header.WAVE, "1")),
            params=headers.get(Header.PARAMS, "").split(),
            key=[k.strip() for k in headers[Header.KEY].split(",")],
            collect=collect[0] if collect else "",
            collect_column=collect[1] if len(collect) > 1 else "",
            fetch_sql=fetch_sql.strip(),
            verify_sql=verify_sql.strip(),
            min_version=gate.min_version,
            max_version=gate.max_version,
            only_gp=gate.only_gp,
            not_gp=gate.not_gp,
        )


class ApplyRow(BaseModel):
    op: str
    planned: int
    applied: int


class Params:
    """
    Параметры scrape-файла: словарь массивов OID по именам из @params; плейсхолдеры в
    файлах
    именованные (%(rels)s::oid[]), в стиле psycopg."""

    @staticmethod
    def of(
        arrays: dict[str, Sequence[int]], names: Sequence[str]
    ) -> dict[str, list[int]]:
        values: dict[str, list[int]] = {}
        for name in names:
            values[name] = list(arrays.get(name, ()))
        return values


class CatalogChangedError(Exception):
    """Каталог источника изменился между чтением и сверкой."""


class Pipeline:
    """
    Один прогон: строки источника потоком уходят в raw_* сессии ix, сверка по xmin через
    временную таблицу и except на стороне ix, затем стадии и apply. В памяти Python
    только
    текущая строка и массивы OID для параметров следующих волн."""

    ITERSIZE = 2000

    def __init__(
        self,
        cfg: WorkerConfig,
        files: Sequence[ScrapeFile],
        layout_dir: Path,
        address: SourceAddress,
    ) -> None:
        self._cfg = cfg
        self._files = files
        self._dir = layout_dir
        self._address = address

    def run(self) -> Sequence[ApplyRow]:
        with (
            psycopg.connect(
                self._cfg.source_dsn, autocommit=True, application_name=self.APP_NAME
            ) as src,
            psycopg.connect(
                self._cfg.ix_dsn, autocommit=True, application_name=self.APP_NAME
            ) as ix,
        ):
            server = self._server(src)
            self._session(src, server, read_only=True)
            self._session(
                ix, ServerInfo(version_num=999999, is_greenplum=False), read_only=False
            )
            chosen = list(self._choose(server))
            ix.execute(self._read(LayoutFile.RAW_SCHEMA))
            ix.execute(
                "insert into raw_source (scheme, host, port, database) values (%s, "
                "%s, %s, %s)",
                (
                    self._address.scheme,
                    self._address.host,
                    self._address.port,
                    self._address.database,
                ),
            )
            arrays: dict[str, Sequence[int]] = {}
            for file in chosen:
                arrays.update(self._stream(src, ix, file, arrays))
            for file in chosen:
                self._verify(src, ix, file, arrays)
            for name in (
                LayoutFile.STAGE,
                LayoutFile.NODES,
                LayoutFile.TREE,
                LayoutFile.EDGES,
                LayoutFile.SURFACES,
            ):
                ix.execute(self._read(name))
            return self._apply(ix)

    LOCK_TIMEOUT_SINCE = 90300
    APP_NAME: ClassVar[str] = "pg-meta-scraper"

    def _session(
        self, conn: psycopg.Connection, server: ServerInfo, read_only: bool
    ) -> None:
        """Настройки сессии; lock_timeout появился в 9.3, statement_timeout есть
        везде."""
        if server.version_num >= self.LOCK_TIMEOUT_SINCE:
            conn.execute(
                sql.SQL("set lock_timeout = {}").format(
                    sql.Literal(self._cfg.lock_timeout)
                )
            )
        conn.execute(
            sql.SQL("set statement_timeout = {}").format(
                sql.Literal(self._cfg.statement_timeout)
            )
        )
        if read_only:
            conn.execute("set default_transaction_read_only = on")

    def _server(self, conn: psycopg.Connection) -> ServerInfo:
        record = conn.execute("show server_version_num").fetchone()
        version_record = conn.execute("select version()").fetchone()
        if record is None or version_record is None:
            raise ScrapeWorkerError(
                "source: expected server_version_num and version(), got none"
            )
        return ServerInfo(
            version_num=int(record[0]),
            is_greenplum=Marker.GREENPLUM in str(version_record[0]),
        )

    def _choose(self, server: ServerInfo) -> Iterator[ScrapeFile]:
        by_name: dict[str, list[ScrapeFile]] = {}
        for file in self._files:
            by_name.setdefault(file.name, []).append(file)
        for name in sorted(by_name, key=lambda n: (by_name[n][0].wave, n)):
            variants = [f for f in by_name[name] if f.applies(server)]
            if len(variants) > 1:
                raise ScrapeWorkerError(
                    f"scrape {name}: {len(variants)} variants apply to server "
                    "{server.version_num}"
                )
            if variants:
                yield variants[0]

    def _stream(
        self,
        src: psycopg.Connection,
        ix: psycopg.Connection,
        file: ScrapeFile,
        arrays: dict[str, Sequence[int]],
    ) -> dict[str, Sequence[int]]:
        """Выборка одного файла: серверный курсор на источнике, COPY в raw_<name>,
        попутно массив @collect."""
        query = file.fetch_sql.encode("utf-8")
        values = Params.of(arrays, file.params)
        collected: list[int] = []
        count = 0
        with src.transaction(), src.cursor(name=f"scrape_{file.name}") as cur:
            cur.itersize = self.ITERSIZE
            cur.execute(query, values)
            columns = [d.name for d in cur.description or ()]
            position = columns.index(file.collect_column) if file.collect else -1
            target = sql.Identifier(f"raw_{file.name}")
            with ix.cursor().copy(
                sql.SQL("copy {} ({}) from stdin").format(
                    target, sql.SQL(", ").join(sql.Identifier(c) for c in columns)
                )
            ) as copy:
                for row in cur:
                    copy.write_row(row)
                    count += 1
                    if position >= 0:
                        collected.append(int(row[position]))
        logger.info("scrape %s (%s): %d rows", file.name, file.path.name, count)
        if not file.collect:
            return {}
        return {file.collect: collected}

    def _verify(
        self,
        src: psycopg.Connection,
        ix: psycopg.Connection,
        file: ScrapeFile,
        arrays: dict[str, Sequence[int]],
    ) -> None:
        """Сверка: строки @verify потоком в verify_<name>, сравнение с raw_<name> на
        стороне ix."""
        query = file.verify_sql.encode("utf-8")
        values = Params.of(arrays, file.params)
        keys = sql.SQL(", ").join(sql.Identifier(c) for c in [*file.key, "row_xmin"])
        raw = sql.Identifier(f"raw_{file.name}")
        check = sql.Identifier(f"verify_{file.name}")
        ix.execute(
            sql.SQL("create temp table {} as select {} from {} where false").format(
                check, keys, raw
            )
        )
        with src.transaction(), src.cursor(name=f"verify_{file.name}") as cur:
            cur.itersize = self.ITERSIZE
            cur.execute(query, values)
            with ix.cursor().copy(
                sql.SQL("copy {} ({}) from stdin").format(check, keys)
            ) as copy:
                for row in cur:
                    copy.write_row(row)
        diff = ix.execute(
            sql.SQL(
                "select count(*) from ((select {k} from {r} except all select {k} "
                "from {c}) union all (select {k} from {c} except all select {k} from "
                "{r})) d"
            ).format(k=keys, r=raw, c=check)
        ).fetchone()
        ix.execute(sql.SQL("drop table {}").format(check))
        if diff is None or int(diff[0]) != 0:
            raise CatalogChangedError(file.name)

    def _apply(self, ix: psycopg.Connection) -> Sequence[ApplyRow]:
        try:
            ix.execute(self._read(LayoutFile.LOCK))
            ix.execute("begin isolation level repeatable read")
            try:
                rows = self._last_result(ix.execute(self._read(LayoutFile.APPLY)))
                ix.execute("commit")
            except Exception:
                ix.execute("rollback")
                raise
        finally:
            ix.execute(self._read(LayoutFile.UNLOCK))
        summary: list[ApplyRow] = []
        for row in rows:
            summary.append(
                ApplyRow.model_validate(
                    {"op": row[0], "planned": row[1], "applied": row[2]}
                )
            )
        return summary

    @staticmethod
    def _last_result(cur: psycopg.Cursor) -> list[tuple[object, ...]]:
        """Скрипт из многих statement'ов: сводка это последний набор строк."""
        rows: list[tuple[object, ...]] = []
        while True:
            if cur.description is not None:
                rows = [tuple(r) for r in cur.fetchall()]
            if not cur.nextset():
                return rows

    def _read(self, name: LayoutFile) -> bytes:
        text = (self._dir / name).read_text(encoding="utf-8")
        return SchemaName.render(text, self._cfg.db_schema)


class ScrapeWorker:
    """
    Полный прогон одного источника с повторами по README, шаг 4 и шаг 8. Каждая попытка
    открывает обе сессии заново: временные raw_* предыдущей попытки исчезают вместе с
    сессией."""

    def __init__(self, cfg: WorkerConfig, package_dir: Path) -> None:
        self._cfg = cfg
        self._files = [
            ScrapeFile.parse(p) for p in sorted((package_dir / "scrape").glob("*.sql"))
        ]
        self._layout_dir = package_dir / "layout"
        self._address = SourceAddress.of_dsn(cfg.source_dsn)

    def run(self) -> Sequence[ApplyRow]:
        last = ""
        for attempt in range(1, self._cfg.attempts + 1):
            pipeline = Pipeline(self._cfg, self._files, self._layout_dir, self._address)
            try:
                summary = pipeline.run()
            except CatalogChangedError as exc:
                last = f"catalog changed during read: {exc}"
            except (LockNotAvailable, SerializationFailure) as exc:
                last = f"ix busy: {exc}".strip()
            except psycopg.Error as exc:
                raise ScrapeWorkerError(
                    f"scrape {self._cfg.source_dsn}: {exc}"
                ) from exc
            else:
                mismatched = [r.op for r in summary if r.planned != r.applied]
                if mismatched:
                    raise ScrapeWorkerError(
                        f"apply: planned <> applied for {mismatched}"
                    )
                return summary
            logger.warning("attempt %d/%d: %s", attempt, self._cfg.attempts, last)
        raise ScrapeWorkerError(
            f"scrape {self._cfg.source_dsn}: {self._cfg.attempts} attempts failed, "
            "last: {last}"
        )


class Command(StrEnum):
    """Что делает запуск: накатить свою схему или снять источники."""

    UPGRADE = "upgrade"
    RUN = "run"


class Cli:
    """Команда и путь к конфигу; настройки берутся из секции [ix.meta_scraper].

    Источник выбирается по имени из той же секции; без --source снимаются все
    перечисленные по порядку.
    """

    SECTION: ClassVar[str] = "ix.meta_scraper"

    @classmethod
    def parse(cls, argv: Sequence[str] | None = None) -> tuple[Command, Path, str]:
        parser = argparse.ArgumentParser(
            prog="boba-pg-meta-scraper",
            description=(
                "Снятие каталога PostgreSQL или Greenplum в граф ix: схема пакета, "
                "scrape, раскладка, merge."
            ),
        )
        parser.add_argument(
            "command",
            type=Command,
            choices=list(Command),
            help=(
                "upgrade — накатить схему пакета в базу ix (идемпотентно, ядро "
                "должно быть уже накачено пакетом pg-ix-core); run — снять каталог."
            ),
        )
        parser.add_argument(
            "--config",
            required=True,
            type=Path,
            help=(
                "Путь к файлу конфига приложения (toml). База ix, источники и "
                "границы прогона берутся из секции [ix.meta_scraper]."
            ),
        )
        parser.add_argument(
            "--source",
            default="",
            help=(
                "Имя источника из [ix.meta_scraper].sources. Пусто — снять все "
                "источники секции по порядку."
            ),
        )
        args = parser.parse_args(argv)

        return args.command, args.config, args.source


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    package_dir = Path(__file__).resolve().parent
    try:
        command, config_path, selected = Cli.parse()

        if command is Command.UPGRADE:
            upgrade = bind_section(config_path, Cli.SECTION, UpgradeConfig)
            report = SchemaUpgrade(package_dir / "schema").run(upgrade)
            logger.info("schema applied: %s", ", ".join(report.files))
            return

        config = bind_section(config_path, Cli.SECTION, ScraperConfig)

        sources = list(config.sources)
        if selected:
            sources = [config.source(selected)]

        for source in sources:
            logger.info("source %s: scrape started", source.name)
            summary = ScrapeWorker(config.run_of(source), package_dir).run()
            for row in summary:
                logger.info(
                    "source %s: %s planned=%d applied=%d",
                    source.name,
                    row.op,
                    row.planned,
                    row.applied,
                )
    except (ConfigError, SchemaUpgradeError, ScrapeWorkerError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
