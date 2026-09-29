"""Приёмники насосов UI-стенда на серверах [ix_stand]: база ClickHouse и
схема Oracle, куда модель в ходе чата льёт потоки.

Общий кластер ClickHouse приложения и схемы скраперов стенд не трогает: база
создаётся на первом demo-источнике ch_sources профилем без readonly-настроек,
схема Oracle — отдельным пользователем на первом ora_sources. Оба имени
несут метку набора и процесса (StandNames) и сносятся после модуля.

Ошибки:
StreamSinksError — в [ix_stand] нет demo-ClickHouse или Oracle.
OracleQueryError — Oracle отклонил DDL администратора.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from boba.db.clickhouse.connection import ClickHouseConfig
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.query import ChQueryBuilder
from boba.db.oracle.connection import OracleConfig
from boba.db.oracle.payload import PayloadOracle
from boba.db.oracle.query import OraIdentifier, OraIdentifiers, OraQueryBuilder
from boba.stand.edm import EdmSource
from boba.stand.names import StandNames
from boba.stand.oracle_user import StandOracleUser

__all__ = ["SinkSources", "StreamSinks", "StreamSinksError"]


class StreamSinksError(RuntimeError):
    """В конфиге стенда нет серверов для приёмников насосов."""


class OraSinkSource(BaseModel):
    """Источник Oracle из [ix_stand].ora_sources: нужен профиль администратора."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    admin: OracleConfig


class SinkSources(BaseModel):
    """Секция [ix_stand]: списки ClickHouse и Oracle."""

    model_config = ConfigDict(extra="ignore")

    ch_sources: Sequence[EdmSource]
    ora_sources: Sequence[OraSinkSource]


class StreamSinks:
    """База ClickHouse и схема Oracle для приёмников насосов UI-стенда.

    recreate пересоздаёт обе, drop сносит; профили ch_connection и
    ora_connection сеятель стенда заводит соединениями, которые модель
    называет в вызовах приёмников.
    """

    CH_DATABASE: ClassVar[str] = "ui_stream"
    ORA_USER: ClassVar[str] = "UI_STREAM"

    def __init__(self, sources: SinkSources) -> None:
        self._ch = self._first_demo(sources.ch_sources)
        self._database = StandNames().of(self.CH_DATABASE)

        if not sources.ora_sources:
            msg = (
                "stand config: expected an oracle source in [ix_stand].ora_sources "
                "for the stream sinks, got none"
            )
            raise StreamSinksError(msg)

        self._ora = StandOracleUser(sources.ora_sources[0].admin, self.ORA_USER)

    @property
    def ch_database(self) -> str:
        return self._database

    @property
    def ora_schema(self) -> str:
        return self._ora.name

    @property
    def ch_connection(self) -> ClickHouseConfig:
        return self._ch.admin

    @property
    def ora_connection(self) -> OracleConfig:
        return self._ora.owner

    async def recreate(self) -> None:
        async with PayloadClickHouse.opened_config(self._ch.admin) as client:
            await self._command(client, "drop database if exists {db:Identifier}")
            await self._command(client, "create database {db:Identifier}")

        await self._ora.recreate()

    async def drop(self) -> None:
        async with PayloadClickHouse.opened_config(self._ch.admin) as client:
            await self._command(client, "drop database if exists {db:Identifier}")

        await self._ora.drop()

    async def ch_rows(
        self, table: str, columns: Sequence[str]
    ) -> list[tuple[Any, ...]]:
        """Строки таблицы базы приёмника по порядку id: колонки columns."""
        query = (
            ChQueryBuilder()
            .add("select")
            .raw_query(", ".join(columns))
            .add(
                "from {db:Identifier}.{table:Identifier} order by id",
                db=self._database,
                table=table,
            )
            .build()
        )
        async with PayloadClickHouse.opened_config(self._ch.admin) as client:
            result = await client.query(query.text, parameters=query.params)

        rows: list[tuple[Any, ...]] = []
        for row in result.result_rows:
            rows.append(tuple(row))

        return rows

    async def ora_rows(
        self, table: str, columns: Sequence[str]
    ) -> list[tuple[Any, ...]]:
        """Строки таблицы схемы приёмника по порядку id: колонки columns."""
        payload = PayloadOracle(self._ora.owner)
        query = (
            OraQueryBuilder()
            .add("select ", OraIdentifiers(columns))
            .add("from ", OraIdentifier(table), " order by id")
            .build()
        )

        rows: list[tuple[Any, ...]] = []
        async with payload.opened() as conn, payload.rows(conn, query.text) as stream:
            async for block in stream.blocks:
                rows.append(tuple(block))

        return rows

    async def _command(self, client: Any, text: str) -> None:
        query = ChQueryBuilder().add(text, db=self._database).build()
        await client.command(query.text, parameters=query.params)

    @staticmethod
    def _first_demo(sources: Sequence[EdmSource]) -> EdmSource:
        for source in sources:
            if source.demo:
                return source

        msg = (
            "stand config: expected a demo = true source in [ix_stand].ch_sources "
            "for the stream sinks, got none"
        )
        raise StreamSinksError(msg)
