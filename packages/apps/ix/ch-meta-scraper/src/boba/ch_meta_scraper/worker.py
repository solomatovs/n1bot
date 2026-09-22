"""Источник ClickHouse для общего цикла скрапера boba.ix_core.scrape: клиент
HTTP-интерфейса по профилю boba-db-clickhouse, ворота файлов по версии сервера,
строки запроса потоком. Всё по README пакета.

Ошибки:
ScrapeSourceError — сервер недоступен (сеть, kerberos, вход) или отклонил запрос.
ScrapeWorkerError — из ядра: ix недоступен, контракт файлов нарушен, попытки
    исчерпаны.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from clickhouse_connect.driver.asyncclient import AsyncClient

from boba.db.clickhouse import ClickHouseError, ClickHouseQueryError
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.profile import ClickHouseConfig
from boba.ix_core.scrape import (
    ScraperConfigBase,
    ScrapeSession,
    ScrapeSource,
    ScrapeSourceError,
    SourceAddressBase,
    SourceConfigBase,
    SourceRows,
    StreamRows,
    parse_version,
    run_cli,
    version_applies,
)

__all__ = [
    "ChSource",
    "ScraperConfig",
    "SourceAddress",
    "SourceConfig",
    "read_server_version",
]

SCHEME = "clickhouse"
VERSION_QUERY = "select version()"
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


class SourceAddress(SourceAddressBase):
    scheme: str = SCHEME


def source_address(clickhouse: ClickHouseConfig) -> SourceAddress:
    if clickhouse.host is None:
        raise ScrapeSourceError("source clickhouse: expected host in the profile")

    if clickhouse.port is None:
        raise ScrapeSourceError("source clickhouse: expected port in the profile")

    return SourceAddress(host=clickhouse.host, port=clickhouse.port)


async def read_server_version(client: AsyncClient, where: str) -> tuple[int, ...]:
    try:
        result = await client.query(VERSION_QUERY)
    except ClickHouseQueryError as exc:
        raise ScrapeSourceError(f"{VERSION_QUERY} on {where}: {exc}") from exc

    for row in result.result_rows:
        return parse_version(str(row[0]))

    raise ScrapeSourceError(f"{VERSION_QUERY} on {where}: expected one row, got none")


class ChSession(ScrapeSession):
    """Сессия источника: открытый клиент и версия сервера."""

    def __init__(
        self, client: AsyncClient, server: tuple[int, ...], where: str
    ) -> None:
        self._client = client
        self._server = server
        self._where = where

    def applies(self, headers: Mapping[str, str]) -> bool:
        return version_applies(headers, self._server)

    @asynccontextmanager
    async def fetch_rows(
        self, name: str, query: str, params: Mapping[str, Sequence[object]]
    ) -> AsyncGenerator[SourceRows, None]:
        try:
            async with PayloadClickHouse.rows(self._client, query, params) as stream:
                yield StreamRows(
                    stream.names,
                    stream.blocks,
                    name,
                    self._where,
                    (ClickHouseQueryError,),
                )
        except ClickHouseQueryError as exc:
            raise ScrapeSourceError(f"query {name} on {self._where}: {exc}") from exc


class ChSource(ScrapeSource):
    """Реализация ScrapeSource для ClickHouse: клиент HTTP-интерфейса по профилю
    boba-db-clickhouse, kerberos-окружение держится всю сессию."""

    def __init__(self, cfg: ClickHouseConfig) -> None:
        self._cfg = cfg
        self._address = source_address(cfg)

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


def main() -> None:
    package_dir = Path(__file__).resolve().parent
    run_cli(PROG, DESCRIPTION, SECTION, package_dir, ScraperConfig)


if __name__ == "__main__":
    main()
