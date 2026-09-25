"""Тела насосов трёх баз, вызванные напрямую с профилями стенда: порты в
памяти, стейтменты целиком, как их писала бы LLM."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from boba.db.clickhouse.connection import ClickHouseConfig
from boba.db.oracle.connection import OracleConfig
from boba.db.postgres.connection import PostgresConfig
from boba.pump_stand.ports import Feed, Sink
from boba.tool.ch import tools as ch
from boba.tool.ora import tools as ora
from boba.tool.pg import tools as pg
from boba.toolkit.entry import ToolMain

__all__ = ["Pumps"]

Body = Callable[..., Awaitable[Any]]


class Pumps:
    """Насосы postgres, ClickHouse и Oracle над профилями стенда. Выход
    возвращает байты порта, вход принимает байты и отдаёт текст отчёта."""

    CH_CHUNK_BYTES: ClassVar[int] = 4096
    """Размер блока насосов ClickHouse: нижняя граница фасада."""

    def __init__(
        self,
        postgres: PostgresConfig | None = None,
        clickhouse: ClickHouseConfig | None = None,
        oracle: OracleConfig | None = None,
        chunk: int = 777,
    ) -> None:
        self._pg = postgres
        self._ch = clickhouse
        self._ora = oracle
        self._chunk = chunk
        self._bodies: dict[str, Body] = {}
        listed = ToolMain.toolset(
            pg.pg_copy_out,
            pg.pg_copy_in,
            ch.ch_stream_out,
            ch.ch_stream_in,
            ora.ora_copy_out,
            ora.ora_copy_in,
        )
        for payload in listed:
            if payload.coroutine is None:
                raise AssertionError(f"{payload.name}: body is None")

            self._bodies[payload.name] = payload.coroutine

    async def pg_out(self, statement: str) -> bytes:
        return await self._out("pg_copy_out", self._pg, statement)

    async def pg_in(self, statement: str, data: bytes, chunk: int | None = None) -> str:
        return await self._in("pg_copy_in", self._pg, statement, data, chunk)

    async def ch_out(self, statement: str) -> bytes:
        return await self._out(
            "ch_stream_out", self._ch, statement, chunk_bytes=self.CH_CHUNK_BYTES
        )

    async def ch_in(self, statement: str, data: bytes, chunk: int | None = None) -> str:
        return await self._in(
            "ch_stream_in",
            self._ch,
            statement,
            data,
            chunk,
            chunk_bytes=self.CH_CHUNK_BYTES,
        )

    async def ora_out(self, statement: str) -> bytes:
        return await self._out("ora_copy_out", self._ora, statement)

    async def ora_in(self, table: str, columns: list[str], data: bytes) -> str:
        report = await self._bodies["ora_copy_in"](
            connection=self._required("ora_copy_in", self._ora),
            table=table,
            columns=columns,
            feed=Feed(data, self._chunk),
        )

        return report.text

    async def _out(
        self, name: str, connection: object, statement: str, **extra: Any
    ) -> bytes:
        sink = Sink()
        await self._bodies[name](
            connection=self._required(name, connection),
            sql=statement,
            out=sink,
            **extra,
        )

        return sink.data()

    async def _in(
        self,
        name: str,
        connection: object,
        statement: str,
        data: bytes,
        chunk: int | None,
        **extra: Any,
    ) -> str:
        if chunk is None:
            chunk = self._chunk

        report = await self._bodies[name](
            connection=self._required(name, connection),
            sql=statement,
            feed=Feed(data, chunk),
            **extra,
        )

        return report.text

    @staticmethod
    def _required(name: str, connection: object) -> object:
        if connection is None:
            raise AssertionError(f"{name}: the stand has no connection for it")

        return connection
