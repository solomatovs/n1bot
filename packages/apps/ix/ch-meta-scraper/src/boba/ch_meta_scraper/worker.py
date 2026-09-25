"""Источник ClickHouse для общего цикла скрапера boba.ix_core.scrape: клиент
HTTP-интерфейса по профилю boba-db-clickhouse, ворота файлов по версии сервера,
строки запроса потоком. Всё по README пакета.

Ошибки:
ScrapeSourceError — сервер недоступен (сеть, kerberos, вход) или отклонил запрос.
ScrapeWorkerError — из ядра: ix недоступен, контракт файлов нарушен, попытки
    исчерпаны.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clickhouse_connect.driver.asyncclient import AsyncClient

from boba.db.clickhouse import ClickHouseError, ClickHouseQueryError
from boba.db.clickhouse.connection import ClickHouseConfig
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.query import ChQueryBuilder
from boba.ix_core.scrape import (
    BlockStream,
    Collect,
    CopyFormat,
    ScrapeFile,
    ScraperConfigBase,
    ScrapeSession,
    ScrapeSource,
    ScrapeSourceError,
    SourceAddressBase,
    SourceBlocks,
    SourceConfigBase,
    parse_version,
    run_cli,
)

__all__ = [
    "ChSource",
    "ScraperConfig",
    "SourceAddress",
    "SourceConfig",
    "read_server_version",
]

SCHEME = "clickhouse"
SECTION = "ix.ch_meta_scraper"
PROG = "boba-ch-meta-scraper"
DESCRIPTION = (
    "Снятие каталога ClickHouse в граф ix: схема пакета, scrape, раскладка, merge."
)


class SourceConfig(SourceConfigBase):
    """Источник снятия: имя для выбора из командной строки и профиль подключения."""

    clickhouse: ClickHouseConfig


class ScraperConfig(ScraperConfigBase[SourceConfig]):
    """Секция [ix.ch_meta_scraper]: база ix и список источников. Границы сессии
    источника (readonly, max_execution_time) задаёт settings его профиля."""

    def scrape_source(self, item: SourceConfig) -> ScrapeSource:
        return ChSource(item.clickhouse)


@dataclass(frozen=True, kw_only=True)
class SourceAddress(SourceAddressBase):
    scheme: str = SCHEME


def source_address(clickhouse: ClickHouseConfig) -> SourceAddress:
    if clickhouse.host is None:
        raise ScrapeSourceError("source clickhouse: expected host in the profile")

    if clickhouse.port is None:
        raise ScrapeSourceError("source clickhouse: expected port in the profile")

    return SourceAddress(host=clickhouse.host, port=clickhouse.port)


async def read_server_version(client: AsyncClient, where: str) -> tuple[int, ...]:
    query = "select version()"

    try:
        result = await client.query(query)
    except ClickHouseQueryError as exc:
        raise ScrapeSourceError(f"{query} on {where}: {exc}") from exc

    for row in result.result_rows:
        return parse_version(str(row[0]))

    raise ScrapeSourceError(f"{query} on {where}: expected one row, got none")


class ChSession(ScrapeSession):
    """Сессия источника: открытый клиент и версия сервера."""

    def __init__(
        self, client: AsyncClient, server: tuple[int, ...], where: str
    ) -> None:
        self._client = client
        self._server = server
        self._where = where

    def applies(self, file: ScrapeFile) -> bool:
        return file.applies(self._server, "")

    @asynccontextmanager
    async def fetch_blocks(
        self, name: str, path: Path, params: Mapping[str, Sequence[object]]
    ) -> AsyncGenerator[SourceBlocks, None]:
        """TabSeparated сервера как текстовый формат COPY: даты и время в UTC
        (session_timezone есть с 23.x, раньше действует зона сервера), массивы файлы
        отдают через toJSONString, потому что `['a']` PostgreSQL не разбирает.
        prefer_column_name_to_alias: alias с именем колонки не должен подменять её
        в хэше row_version."""
        query = ChQueryBuilder().read(path, **params).build()
        settings: dict[str, Any] = {
            "output_format_tsv_crlf_end_of_line": 0,
            "prefer_column_name_to_alias": 1,
        }
        if self._server >= (23,):
            settings["session_timezone"] = "UTC"

        label = f"{name} ({path.name}) on {self._where}"
        try:
            async with PayloadClickHouse.tsv_stream_out(
                self._client, query.text, query.params, settings
            ) as stream:
                yield BlockStream(
                    stream.names,
                    CopyFormat.TEXT,
                    stream.blocks,
                    label,
                    (ClickHouseQueryError,),
                )
        except ClickHouseQueryError as exc:
            raise ScrapeSourceError(f"query {label}: {exc}") from exc


class ChSource(ScrapeSource):
    """Реализация ScrapeSource для ClickHouse: клиент HTTP-интерфейса по профилю
    boba-db-clickhouse, kerberos-окружение держится всю сессию."""

    def __init__(self, cfg: ClickHouseConfig) -> None:
        self._cfg = cfg
        self._address = source_address(cfg)

    @property
    def files(self) -> Sequence[ScrapeFile]:
        return (
            ScrapeFile(
                name="databases",
                wave=1,
                query="1_databases.sql",
                collect=Collect(name="dbs", column="name"),
            ),
            ScrapeFile(name="server", wave=1, query="1_server.sql"),
            ScrapeFile(name="columns", wave=2, query="2_columns.sql", params=("dbs",)),
            ScrapeFile(
                name="dictionaries", wave=2, query="2_dictionaries.sql", params=("dbs",)
            ),
            ScrapeFile(name="functions", wave=2, query="2_functions.sql"),
            ScrapeFile(name="indices", wave=2, query="2_indices.sql", params=("dbs",)),
            ScrapeFile(
                name="projections",
                wave=2,
                query="2_projections.sql",
                params=("dbs",),
                min_version=(24, 4),
            ),
            ScrapeFile(
                name="tables",
                wave=2,
                query="2_tables.sql",
                params=("dbs",),
                min_version=(26, 6),
            ),
            ScrapeFile(
                name="tables",
                wave=2,
                query="2_tables__lt26_6.sql",
                params=("dbs",),
                max_version=(26, 5),
            ),
        )

    @property
    def address(self) -> SourceAddress:
        return self._address

    def describe(self) -> str:
        return f"{self._cfg.interface}://{self._address.host}:{self._address.port}"

    @asynccontextmanager
    async def open_session(self) -> AsyncGenerator[ScrapeSession, None]:
        try:
            async with PayloadClickHouse.opened_config(self._cfg) as client:
                server = await read_server_version(client, self.describe())
                yield ChSession(client, server, self.describe())
        except ClickHouseError as exc:
            raise ScrapeSourceError(
                f"connecting to {self.describe()} as {self._cfg.trace()}: {exc}"
            ) from exc


async def main() -> None:
    package_dir = Path(__file__).resolve().parent
    await run_cli(PROG, DESCRIPTION, SECTION, package_dir, ScraperConfig)


def cli() -> None:
    """Точка входа консольного скрипта: единственный asyncio.run на процесс."""
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
