"""Источник PostgreSQL/Greenplum для общего цикла скрапера boba.ix_core.scrape:
сессия источника только на чтение с таймаутами, ворота файлов по
server_version_num и признаку Greenplum, серверный курсор на каждый запрос.
Всё по README пакета, шаги 1–8.

Ошибки:
ScrapeSourceError — источник недоступен (сеть, kerberos, libpq) или отклонил
    запрос; ScrapeSourceBusyError — замок или сериализация на источнике, прогон
    повторяется.
ScrapeWorkerError — из ядра: ix недоступен, контракт файлов нарушен, попытки
    исчерпаны.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.errors import LockNotAvailable, SerializationFailure
from pydantic import BaseModel, ConfigDict

from boba.db.postgres import AsyncPostgresPool, PostgresError
from boba.db.postgres.profile import PostgresConfig
from boba.db.postgres.query import PgQueryBuilder
from boba.ix_core.scrape import (
    BlockStream,
    Collect,
    CopyFormat,
    ScrapeFile,
    ScraperConfigBase,
    ScrapeSession,
    ScrapeSource,
    ScrapeSourceBusyError,
    ScrapeSourceError,
    SourceAddressBase,
    SourceBlocks,
    SourceConfigBase,
    run_cli,
)
from boba.kerberos import KerberosError

__all__ = [
    "PgSource",
    "ScraperConfig",
    "ServerInfo",
    "SourceAddress",
    "SourceConfig",
    "WorkerConfig",
    "read_server_info",
    "source_address",
]

SECTION = "ix.meta_scraper"
PROG = "boba-pg-meta-scraper"
DESCRIPTION = (
    "Снятие каталога PostgreSQL или Greenplum в граф ix: схема пакета, "
    "scrape, раскладка, merge."
)


class Marker(StrEnum):
    GP = "gp"
    GREENPLUM = "Greenplum"
    SCHEME = "postgresql"


class SourceConfig(SourceConfigBase):
    """Источник снятия: имя для выбора из командной строки и профиль подключения."""

    postgres: PostgresConfig


class WorkerConfig(BaseModel):
    """Один прогон: откуда снимаем и границы сессии источника."""

    model_config = ConfigDict(frozen=True)

    source: PostgresConfig
    lock_timeout: str = "2s"
    statement_timeout: str = "30s"


class ScraperConfig(ScraperConfigBase[SourceConfig]):
    """Секция [ix.meta_scraper]: база ix, список источников и границы прогона.

    lock_timeout и statement_timeout ограничивают сессию источника; границы сессии
    ix задаёт postgres.options той же секции.
    """

    lock_timeout: str = "2s"
    statement_timeout: str = "30s"

    def scrape_source(self, item: SourceConfig) -> ScrapeSource:
        return PgSource(
            WorkerConfig(
                source=item.postgres,
                lock_timeout=self.lock_timeout,
                statement_timeout=self.statement_timeout,
            )
        )


@dataclass(frozen=True, kw_only=True)
class SourceAddress(SourceAddressBase):
    scheme: str = Marker.SCHEME
    database: str


def source_address(postgres: PostgresConfig) -> SourceAddress:
    host = postgres.host
    if host is None:
        host = postgres.hostaddr

    if host is None:
        raise ScrapeSourceError(
            f"source {postgres.where()}: expected host or hostaddr in the profile"
        )

    if postgres.port is None:
        raise ScrapeSourceError(
            f"source {postgres.where()}: expected port in the profile"
        )

    if postgres.dbname is None:
        raise ScrapeSourceError(
            f"source {postgres.where()}: expected dbname in the profile"
        )

    return SourceAddress(host=host, port=postgres.port, database=postgres.dbname)


@dataclass(frozen=True, kw_only=True)
class ServerInfo:
    """Версия сервера и его вкус для ворот файлов: gp у Greenplum, иначе пусто."""

    version_num: int
    is_greenplum: bool

    def flavor(self) -> str:
        if self.is_greenplum:
            return Marker.GP

        return ""


async def read_server_info(conn: psycopg.AsyncConnection[Any]) -> ServerInfo:
    cur = await conn.execute("show server_version_num")
    record = await cur.fetchone()
    version_cur = await conn.execute("select version()")
    version_record = await version_cur.fetchone()
    if record is None or version_record is None:
        raise ScrapeSourceError(
            "source: expected server_version_num and version(), got none"
        )

    return ServerInfo(
        version_num=int(record[0]),
        is_greenplum=Marker.GREENPLUM in str(version_record[0]),
    )


class PgSession(ScrapeSession):
    """Сессия источника: соединение только на чтение и версия сервера."""

    def __init__(
        self, conn: psycopg.AsyncConnection[Any], server: ServerInfo, where: str
    ) -> None:
        self._conn = conn
        self._server = server
        self._where = where

    @property
    def server(self) -> ServerInfo:
        return self._server

    def applies(self, file: ScrapeFile) -> bool:
        return file.applies((self._server.version_num,), self._server.flavor())

    @asynccontextmanager
    async def fetch_blocks(
        self, name: str, path: Path, params: Mapping[str, Sequence[object]]
    ) -> AsyncGenerator[SourceBlocks, None]:
        """Имена колонок — из пустой выборки того же запроса, данные — блоками
        `COPY (запрос) TO STDOUT` в текстовом формате, без строк на стороне Python."""
        probe = (
            PgQueryBuilder()
            .add("select * from (")
            .read(path, **params)
            .add(") q limit 0")
            .build()
        )
        query = (
            PgQueryBuilder()
            .add("copy (")
            .read(path, **params)
            .add(") to stdout (format text)")
            .build()
        )
        label = f"{name} ({path.name}) on {self._where}"
        async with self._conn.transaction():
            try:
                cur = await self._conn.execute(probe.text, probe.params)
                columns: list[str] = []
                for column in cur.description or ():
                    columns.append(column.name)

                async with self._conn.cursor().copy(query.text, query.params) as copy:
                    yield BlockStream(
                        columns,
                        CopyFormat.TEXT,
                        self._blocks(copy),
                        label,
                        (psycopg.Error,),
                    )
            except (LockNotAvailable, SerializationFailure) as exc:
                raise ScrapeSourceBusyError(f"query {label}: {exc}") from exc
            except psycopg.Error as exc:
                raise ScrapeSourceError(
                    f"query {label}: {type(exc).__name__}: {exc}"
                ) from exc

    async def _blocks(self, copy: psycopg.AsyncCopy) -> AsyncIterator[memoryview]:
        async for block in copy:
            yield memoryview(block)


class PgSource(ScrapeSource):
    """Реализация ScrapeSource для PostgreSQL и Greenplum: отдельное соединение в
    autocommit, lock_timeout (с 9.3) и statement_timeout из конфига, транзакции
    только на чтение."""

    def __init__(self, cfg: WorkerConfig) -> None:
        self._cfg = cfg
        self._address = source_address(cfg.source)

    @property
    def files(self) -> Sequence[ScrapeFile]:
        return (
            ScrapeFile(name="am", wave=1, query="1_am.sql"),
            ScrapeFile(name="database", wave=1, query="1_database.sql"),
            ScrapeFile(
                name="foreign_server",
                wave=1,
                query="1_foreign_server.sql",
                min_version=(90100,),
            ),
            ScrapeFile(name="language", wave=1, query="1_language.sql"),
            ScrapeFile(
                name="namespace",
                wave=1,
                query="1_namespace.sql",
                collect=Collect(name="schemas", column="oid"),
            ),
            ScrapeFile(name="opclass", wave=1, query="1_opclass.sql"),
            ScrapeFile(name="shdescription", wave=1, query="1_shdescription.sql"),
            ScrapeFile(name="tablespace", wave=1, query="1_tablespace.sql"),
            ScrapeFile(
                name="class",
                wave=2,
                query="2_class.sql",
                params=("schemas",),
                collect=Collect(name="rels", column="oid"),
                min_version=(100000,),
                unless="gp",
            ),
            ScrapeFile(
                name="class",
                wave=2,
                query="2_class__91_96.sql",
                params=("schemas",),
                collect=Collect(name="rels", column="oid"),
                min_version=(90100,),
                max_version=(99999,),
                unless="gp",
            ),
            ScrapeFile(
                name="class",
                wave=2,
                query="2_class__gp6.sql",
                params=("schemas",),
                collect=Collect(name="rels", column="oid"),
                max_version=(99999,),
                only="gp",
            ),
            ScrapeFile(
                name="class",
                wave=2,
                query="2_class__gp7.sql",
                params=("schemas",),
                collect=Collect(name="rels", column="oid"),
                min_version=(100000,),
                only="gp",
            ),
            ScrapeFile(
                name="class",
                wave=2,
                query="2_class__lt91.sql",
                params=("schemas",),
                collect=Collect(name="rels", column="oid"),
                max_version=(90099,),
                unless="gp",
            ),
            ScrapeFile(
                name="proc",
                wave=2,
                query="2_proc.sql",
                params=("schemas",),
                collect=Collect(name="procs", column="oid"),
                min_version=(110000,),
            ),
            ScrapeFile(
                name="proc",
                wave=2,
                query="2_proc__lt11.sql",
                params=("schemas",),
                collect=Collect(name="procs", column="oid"),
                max_version=(109999,),
            ),
            ScrapeFile(
                name="type",
                wave=2,
                query="2_type.sql",
                params=("schemas",),
                collect=Collect(name="types", column="oid"),
            ),
            ScrapeFile(
                name="attrdef",
                wave=3,
                query="3_attrdef.sql",
                params=("rels",),
                collect=Collect(name="attrdefs", column="oid"),
            ),
            ScrapeFile(
                name="attribute",
                wave=3,
                query="3_attribute.sql",
                params=("rels",),
                min_version=(120000,),
            ),
            ScrapeFile(
                name="attribute",
                wave=3,
                query="3_attribute__10_11.sql",
                params=("rels",),
                min_version=(100000,),
                max_version=(119999,),
            ),
            ScrapeFile(
                name="attribute",
                wave=3,
                query="3_attribute__lt10.sql",
                params=("rels",),
                max_version=(99999,),
            ),
            ScrapeFile(
                name="constraint",
                wave=3,
                query="3_constraint.sql",
                params=("rels", "types"),
                collect=Collect(name="constraints", column="oid"),
                min_version=(150000,),
            ),
            ScrapeFile(
                name="constraint",
                wave=3,
                query="3_constraint__11_14.sql",
                params=("rels", "types"),
                collect=Collect(name="constraints", column="oid"),
                min_version=(110000,),
                max_version=(149999,),
            ),
            ScrapeFile(
                name="constraint",
                wave=3,
                query="3_constraint__92_10.sql",
                params=("rels", "types"),
                collect=Collect(name="constraints", column="oid"),
                min_version=(90200,),
                max_version=(109999,),
            ),
            ScrapeFile(
                name="constraint",
                wave=3,
                query="3_constraint__lt92.sql",
                params=("rels", "types"),
                collect=Collect(name="constraints", column="oid"),
                max_version=(90199,),
            ),
            ScrapeFile(
                name="enum",
                wave=3,
                query="3_enum.sql",
                params=("types",),
                min_version=(90100,),
            ),
            ScrapeFile(
                name="enum",
                wave=3,
                query="3_enum__lt91.sql",
                params=("types",),
                max_version=(90099,),
            ),
            ScrapeFile(
                name="foreign_table",
                wave=3,
                query="3_foreign_table.sql",
                params=("rels",),
                min_version=(90100,),
            ),
            ScrapeFile(
                name="gp_appendonly",
                wave=3,
                query="3_gp_appendonly.sql",
                params=("rels",),
                max_version=(99999,),
                only="gp",
            ),
            ScrapeFile(
                name="gp_distribution_policy",
                wave=3,
                query="3_gp_distribution_policy.sql",
                params=("rels",),
                only="gp",
            ),
            ScrapeFile(
                name="gp_exttable",
                wave=3,
                query="3_gp_exttable.sql",
                params=("rels",),
                max_version=(99999,),
                only="gp",
            ),
            ScrapeFile(
                name="gp_partition",
                wave=3,
                query="3_gp_partition.sql",
                params=("rels",),
                max_version=(99999,),
                only="gp",
            ),
            ScrapeFile(
                name="gp_partition_rule",
                wave=3,
                query="3_gp_partition_rule.sql",
                params=("rels",),
                max_version=(99999,),
                only="gp",
            ),
            ScrapeFile(
                name="index",
                wave=3,
                query="3_index.sql",
                params=("rels",),
                min_version=(110000,),
            ),
            ScrapeFile(
                name="index",
                wave=3,
                query="3_index__lt11.sql",
                params=("rels",),
                min_version=(90100,),
                max_version=(109999,),
            ),
            ScrapeFile(
                name="index",
                wave=3,
                query="3_index__lt91.sql",
                params=("rels",),
                max_version=(90099,),
            ),
            ScrapeFile(
                name="inherits", wave=3, query="3_inherits.sql", params=("rels",)
            ),
            ScrapeFile(
                name="partitioned_table",
                wave=3,
                query="3_partitioned_table.sql",
                params=("rels",),
                min_version=(110000,),
            ),
            ScrapeFile(
                name="partitioned_table",
                wave=3,
                query="3_partitioned_table__10.sql",
                params=("rels",),
                min_version=(100000,),
                max_version=(109999,),
            ),
            ScrapeFile(
                name="range",
                wave=3,
                query="3_range.sql",
                params=("types",),
                min_version=(90200,),
            ),
            ScrapeFile(name="rewrite", wave=3, query="3_rewrite.sql", params=("rels",)),
            ScrapeFile(
                name="sequence",
                wave=3,
                query="3_sequence.sql",
                params=("rels",),
                min_version=(100000,),
            ),
            ScrapeFile(
                name="statistic_ext",
                wave=3,
                query="3_statistic_ext.sql",
                params=("rels",),
                collect=Collect(name="statistics", column="oid"),
                min_version=(100000,),
            ),
            ScrapeFile(
                name="trigger",
                wave=3,
                query="3_trigger.sql",
                params=("rels",),
                collect=Collect(name="triggers", column="oid"),
                min_version=(130000,),
            ),
            ScrapeFile(
                name="trigger",
                wave=3,
                query="3_trigger__lt13.sql",
                params=("rels",),
                collect=Collect(name="triggers", column="oid"),
                max_version=(129999,),
            ),
            ScrapeFile(
                name="depend",
                wave=4,
                query="4_depend.sql",
                params=("rels", "attrdefs", "procs", "types"),
            ),
            ScrapeFile(
                name="description",
                wave=4,
                query="4_description.sql",
                params=(
                    "rels",
                    "procs",
                    "types",
                    "constraints",
                    "schemas",
                    "triggers",
                    "statistics",
                ),
            ),
        )

    @property
    def address(self) -> SourceAddress:
        return self._address

    def describe(self) -> str:
        return self._cfg.source.where()

    @asynccontextmanager
    async def open_session(self) -> AsyncGenerator[ScrapeSession, None]:
        try:
            conn = await AsyncPostgresPool.dedicated(self._cfg.source)
        except (psycopg.Error, PostgresError, KerberosError) as exc:
            raise ScrapeSourceError(
                f"connecting to {self.describe()} as {self._cfg.source.trace()}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        async with conn:
            try:
                server = await read_server_info(conn)
                await self._configure(conn, server)
            except psycopg.Error as exc:
                raise ScrapeSourceError(
                    f"preparing session on {self.describe()}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            yield PgSession(conn, server, self.describe())

    async def _configure(
        self, conn: psycopg.AsyncConnection[Any], server: ServerInfo
    ) -> None:
        lock_timeout_since = 90300
        if server.version_num >= lock_timeout_since:
            query = (
                PgQueryBuilder()
                .add("set lock_timeout = {t}", t=sql.Literal(self._cfg.lock_timeout))
                .build()
            )
            await conn.execute(query.text, query.params)

        query = (
            PgQueryBuilder()
            .add(
                "set statement_timeout = {t}",
                t=sql.Literal(self._cfg.statement_timeout),
            )
            .build()
        )
        await conn.execute(query.text, query.params)
        await conn.execute("set default_transaction_read_only = on")
        await conn.execute("set timezone to 'UTC'")
        await conn.execute("set datestyle to 'ISO, YMD'")
        await conn.execute("set intervalstyle to 'postgres'")
        await conn.execute("set extra_float_digits to 3")
        await conn.execute("set bytea_output to 'hex'")


async def main() -> None:
    package_dir = Path(__file__).resolve().parent
    await run_cli(PROG, DESCRIPTION, SECTION, package_dir, ScraperConfig)


def cli() -> None:
    """Точка входа консольного скрипта: единственный asyncio.run на процесс."""
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
