"""Источник Oracle для общего цикла скрапера boba.ix_core.scrape: одно thin-соединение
python-oracledb на попытку, ворота файлов по версии словаря из sys.registry$, строки
запроса потоком. Всё по README пакета.

Ошибки:
ScrapeSourceError — сервер недоступен (сеть, listener, вход) или отклонил запрос.
ScrapeWorkerError — из ядра: ix недоступен, контракт файлов нарушен, попытки
    исчерпаны.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from oracledb import AsyncConnection
from pydantic import BaseModel, ConfigDict

from boba.db.oracle import OracleError, OracleQueryError
from boba.db.oracle.payload import PayloadOracle, RowStream
from boba.db.oracle.profile import OracleConfig
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
    "OraSource",
    "ScrapeWorker",
    "ScrapeWorkerError",
    "ScraperConfig",
    "ServerVersion",
    "SourceAddress",
    "SourceConfig",
    "VersionGate",
]


class GateHeader(StrEnum):
    """Заголовки ворот файла по версии сервера: `-- @min 12.1`, `-- @max 21`."""

    MIN = "min"
    MAX = "max"


class Marker(StrEnum):
    SCHEME = "oracle"
    VERSION_QUERY = "select version from sys.registry$ where cid = 'CATALOG'"
    VERSION_SEPARATOR = "."


class SourceConfig(SourceConfigBase):
    """Источник снятия: имя для выбора из командной строки и профиль подключения."""

    oracle: OracleConfig


class ScraperConfig(ScraperConfigBase[SourceConfig]):
    """Секция [ix.ora_meta_scraper]: база ix и список источников. Границы сессии
    источника (connect_timeout, call_timeout) задаёт его профиль."""

    def scrape_source(self, item: SourceConfig) -> ScrapeSource:
        return OraSource(item.oracle)


class SourceAddress(SourceAddressBase):
    """Scope источника: сервис, к которому идёт соединение, лежит в database."""

    scheme: str = Marker.SCHEME
    database: str

    @classmethod
    def of(cls, oracle: OracleConfig) -> SourceAddress:
        return cls(host=oracle.host, port=oracle.port, database=oracle.service)


class ServerVersion(BaseModel):
    """Версия словаря кортежем чисел: 12.2.0.1.0 -> (12, 2, 0, 1, 0). Ворота
    сравниваются по своей длине: @max 19 отсекает 21.0.0.0.0, но пропускает 19.3."""

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


class OraRows(SourceRows):
    """Строки потока python-oracledb; отказ сервера уходит ScrapeSourceError."""

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
        except OracleQueryError as exc:
            raise ScrapeSourceError(
                f"reading {self._name} from {self._where}: {exc}"
            ) from exc


class OraSession(ScrapeSession):
    """Сессия источника: открытое соединение и версия словаря."""

    def __init__(
        self, conn: AsyncConnection, server: ServerVersion, where: str
    ) -> None:
        self._conn = conn
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
        if params:
            listed = ", ".join(params)
            raise ScrapeSourceError(
                f"query {name} on {self._where}: oracle scrape files take no "
                f"@params (lists are not bindable), got {listed}"
            )

        try:
            async with PayloadOracle.rows(self._conn, query) as stream:
                yield OraRows(stream, name, self._where)
        except OracleQueryError as exc:
            raise ScrapeSourceError(f"query {name} on {self._where}: {exc}") from exc


class OraSource(ScrapeSource):
    """Реализация ScrapeSource для Oracle: thin-соединение по профилю boba-db-oracle,
    словарь читается из системных таблиц SYS.*$ по точечным грантам."""

    def __init__(self, cfg: OracleConfig) -> None:
        self._cfg = cfg
        self._address = SourceAddress.of(cfg)

    @property
    def address(self) -> SourceAddress:
        return self._address

    def where(self) -> str:
        return self._cfg.where()

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[ScrapeSession, None]:
        try:
            async with PayloadOracle.opened_config(self._cfg) as conn:
                server = await self._version(conn)
                yield OraSession(conn, server, self.where())
        except OracleError as exc:
            raise ScrapeSourceError(
                f"connecting to {self.where()} as {self._cfg.trace()}: {exc}"
            ) from exc

    async def _version(self, conn: AsyncConnection) -> ServerVersion:
        try:
            async with PayloadOracle.rows(conn, Marker.VERSION_QUERY) as stream:
                rows = [row async for row in stream.blocks]
        except OracleQueryError as exc:
            raise ScrapeSourceError(
                f"{Marker.VERSION_QUERY} on {self.where()}: {exc}"
            ) from exc

        if not rows:
            raise ScrapeSourceError(
                f"{Marker.VERSION_QUERY} on {self.where()}: expected one row, got none"
            )

        return ServerVersion.parse(str(rows[0][0]))


class Cli:
    """Секция и подпись команды пакета; сама команда собирается ядром."""

    SECTION: ClassVar[str] = "ix.ora_meta_scraper"
    PROG: ClassVar[str] = "boba-ora-meta-scraper"
    DESCRIPTION: ClassVar[str] = (
        "Снятие словаря Oracle в граф ix: схема пакета, scrape, раскладка, merge."
    )


def main() -> None:
    package_dir = Path(__file__).resolve().parent
    app = ScrapeApp(Cli.PROG, Cli.DESCRIPTION, Cli.SECTION, package_dir, ScraperConfig)
    app.main()


if __name__ == "__main__":
    main()
