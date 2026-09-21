"""Источник ClickHouse для общего цикла скрапера boba.ix_core.scrape: один
HTTP-клиент clickhouse-connect на попытку (пароль, сертификат или kerberos на
каждый запрос), ворота файлов по version() сервера, строки запроса потоком с
серверными параметрами {name:Type}. Всё по README пакета.

Ошибки:
ScrapeSourceError — сервер недоступен (сеть, TLS, kerberos) или отклонил запрос.
ScrapeWorkerError — из ядра: ix недоступен, контракт файлов нарушен, попытки
    исчерпаны.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from clickhouse_connect.driver.asyncclient import AsyncClient
from pydantic import BaseModel, ConfigDict

from boba.db.clickhouse import ClickHouseError, ClickHouseQueryError
from boba.db.clickhouse.payload import PayloadClickHouse, RowStream
from boba.db.clickhouse.profile import ClickHouseConfig
from boba.ix_core.scrape import (
    ApplyRow,
    ScrapeApp,
    ScraperConfigBase,
    ScrapeSession,
    ScrapeSource,
    ScrapeSourceError,
    ScrapeWorker,
    ScrapeWorkerError,
    SourceAddressBase,
    SourceConfigBase,
    SourceRows,
)

__all__ = [
    "ApplyRow",
    "ChSource",
    "ScrapeWorker",
    "ScrapeWorkerError",
    "ScraperConfig",
    "ServerVersion",
    "SourceAddress",
    "SourceConfig",
    "VersionGate",
]


class GateHeader(StrEnum):
    """Заголовки ворот файла по версии сервера: `-- @min 24.4`, `-- @max 26.5`."""

    MIN = "min"
    MAX = "max"


class Marker(StrEnum):
    SCHEME = "clickhouse"
    VERSION_QUERY = "select version()"
    VERSION_SEPARATOR = "."


class SourceConfig(SourceConfigBase):
    """Источник снятия: имя для выбора из командной строки и профиль подключения."""

    clickhouse: ClickHouseConfig


class ScraperConfig(ScraperConfigBase[SourceConfig]):
    """Секция [ix.ch_meta_scraper]: база ix и список источников. Границы сессии
    источника (readonly, max_execution_time) задаёт settings его профиля."""

    def scrape_source(self, item: SourceConfig) -> ScrapeSource:
        return ChSource(item.clickhouse)


class SourceAddress(SourceAddressBase):
    scheme: str = Marker.SCHEME

    @classmethod
    def of(cls, clickhouse: ClickHouseConfig) -> SourceAddress:
        if clickhouse.host is None:
            raise ScrapeSourceError("source clickhouse: expected host in the profile")

        if clickhouse.port is None:
            raise ScrapeSourceError("source clickhouse: expected port in the profile")

        return cls(host=clickhouse.host, port=clickhouse.port)


class ServerVersion(BaseModel):
    """Версия сервера кортежем чисел: 26.3.1.896 -> (26, 3, 1, 896). Ворота
    сравниваются по своей длине: @max 26.5 отсекает 26.6.1.1, но пропускает 26.5.3."""

    model_config = ConfigDict(frozen=True)

    parts: tuple[int, ...]

    @classmethod
    def parse(cls, raw: str) -> ServerVersion:
        parts: list[int] = []
        for piece in raw.strip().split(Marker.VERSION_SEPARATOR):
            if not piece.isdigit():
                raise ScrapeSourceError(
                    f"server version: expected dotted numbers, got {raw!r}"
                )
            parts.append(int(piece))

        if not parts:
            raise ScrapeSourceError(
                f"server version: expected dotted numbers, got {raw!r}"
            )

        return cls(parts=tuple(parts))

    def at_least(self, gate: ServerVersion) -> bool:
        return self.parts[: len(gate.parts)] >= gate.parts

    def at_most(self, gate: ServerVersion) -> bool:
        return self.parts[: len(gate.parts)] <= gate.parts

    def render(self) -> str:
        return Marker.VERSION_SEPARATOR.join(str(p) for p in self.parts)


class VersionGate(BaseModel):
    """Ворота файла по версии: заголовки @min и @max. Базовый класс для DDL стенда
    в тестах."""

    model_config = ConfigDict(frozen=True)

    min_version: ServerVersion = ServerVersion(parts=(0,))
    max_version: ServerVersion = ServerVersion(parts=(999999,))

    @classmethod
    def gate_of(cls, headers: Mapping[str, str]) -> VersionGate:
        gate = VersionGate()
        if low := headers.get(GateHeader.MIN):
            gate = gate.model_copy(update={"min_version": ServerVersion.parse(low)})

        if high := headers.get(GateHeader.MAX):
            gate = gate.model_copy(update={"max_version": ServerVersion.parse(high)})

        return gate

    def applies(self, server: ServerVersion) -> bool:
        if not server.at_least(self.min_version):
            return False

        return server.at_most(self.max_version)


class ChRows(SourceRows):
    """Строки потока clickhouse-connect; отказ сервера уходит ScrapeSourceError."""

    def __init__(self, stream: RowStream, name: str, where: str) -> None:
        self._stream = stream
        self._name = name
        self._where = where

    @property
    def columns(self) -> Sequence[str]:
        return self._stream.names

    async def __aiter__(self) -> AsyncIterator[Sequence[object]]:
        try:
            async for row in self._stream.blocks:
                yield row
        except ClickHouseQueryError as exc:
            raise ScrapeSourceError(
                f"reading {self._name} from {self._where}: {exc}"
            ) from exc


class ChSession(ScrapeSession):
    """Сессия источника: открытый клиент и версия сервера."""

    def __init__(self, client: AsyncClient, server: ServerVersion, where: str) -> None:
        self._client = client
        self._server = server
        self._where = where

    @property
    def server(self) -> ServerVersion:
        return self._server

    def applies(self, headers: Mapping[str, str]) -> bool:
        return VersionGate.gate_of(headers).applies(self._server)

    @asynccontextmanager
    async def rows(
        self, name: str, query: str, params: Mapping[str, Sequence[object]]
    ) -> AsyncGenerator[SourceRows, None]:
        try:
            async with PayloadClickHouse.rows(self._client, query, params) as stream:
                yield ChRows(stream, name, self._where)
        except ClickHouseQueryError as exc:
            raise ScrapeSourceError(f"query {name} on {self._where}: {exc}") from exc


class ChSource(ScrapeSource):
    """Реализация ScrapeSource для ClickHouse: клиент HTTP-интерфейса по профилю
    boba-db-clickhouse, kerberos-окружение держится всю сессию."""

    WHERE: ClassVar[str] = "{scheme}://{host}:{port}"

    def __init__(self, cfg: ClickHouseConfig) -> None:
        self._cfg = cfg
        self._address = SourceAddress.of(cfg)

    @property
    def address(self) -> SourceAddress:
        return self._address

    def where(self) -> str:
        return self.WHERE.format(
            scheme=self._cfg.interface,
            host=self._address.host,
            port=self._address.port,
        )

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[ScrapeSession, None]:
        try:
            async with PayloadClickHouse.opened_config(self._cfg) as client:
                server = await self._version(client)
                yield ChSession(client, server, self.where())
        except ClickHouseError as exc:
            raise ScrapeSourceError(
                f"connecting to {self.where()} as {self._cfg.trace()}: {exc}"
            ) from exc

    async def _version(self, client: AsyncClient) -> ServerVersion:
        try:
            result = await client.query(Marker.VERSION_QUERY)
        except ClickHouseQueryError as exc:
            raise ScrapeSourceError(
                f"{Marker.VERSION_QUERY} on {self.where()}: {exc}"
            ) from exc

        rows = list(result.result_rows)
        if not rows:
            raise ScrapeSourceError(
                f"{Marker.VERSION_QUERY} on {self.where()}: expected one row, got none"
            )

        return ServerVersion.parse(str(rows[0][0]))


class Cli:
    """Секция и подпись команды пакета; сама команда собирается ядром."""

    SECTION: ClassVar[str] = "ix.ch_meta_scraper"
    PROG: ClassVar[str] = "boba-ch-meta-scraper"
    DESCRIPTION: ClassVar[str] = (
        "Снятие каталога ClickHouse в граф ix: схема пакета, scrape, раскладка, merge."
    )


def main() -> None:
    package_dir = Path(__file__).resolve().parent
    app = ScrapeApp(Cli.PROG, Cli.DESCRIPTION, Cli.SECTION, package_dir, ScraperConfig)
    app.main()


if __name__ == "__main__":
    main()
