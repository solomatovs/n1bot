"""Тела насосов трёх баз, вызванные напрямую с профилями стенда: порты в
памяти или труба ОС между двумя насосами, стейтменты целиком, как их писала
бы LLM."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from boba.db.clickhouse.connection import ClickHouseConfig
from boba.db.oracle.connection import OracleConfig
from boba.db.postgres.connection import PostgresConfig
from boba.pump_stand.ports import Feed, Pipe, Sink
from boba.tool.ch import tools as ch
from boba.tool.ora import tools as ora
from boba.tool.pg import tools as pg
from boba.toolkit.entry import ToolMain

__all__ = ["Chained", "Pumps"]

Body = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class Chained:
    """Итог цепочки двух насосов через трубу: отчёты обоих и время целиком."""

    out_report: str
    in_report: str
    seconds: float


class Pumps:
    """Насосы postgres, ClickHouse и Oracle над профилями стенда. Выход
    возвращает байты порта, вход принимает байты и отдаёт текст отчёта;
    chain соединяет два насоса трубой и гонит их одновременно."""

    CHUNK_BYTES: ClassVar[int] = 4096
    """Размер порции насосов с chunk_bytes: нижняя граница фасада."""

    def __init__(
        self,
        postgres: PostgresConfig | None = None,
        clickhouse: ClickHouseConfig | None = None,
        oracle: OracleConfig | None = None,
        chunk: int = 777,
    ) -> None:
        self._chunk = chunk
        self._connections: dict[str, object | None] = {
            "pg_stream_out": postgres,
            "pg_stream_in": postgres,
            "ch_stream_out": clickhouse,
            "ch_stream_in": clickhouse,
            "ora_csv_out": oracle,
            "ora_csv_in": oracle,
        }
        self._bodies: dict[str, Body] = {}
        listed = ToolMain.toolset(
            pg.pg_stream_out,
            pg.pg_stream_in,
            ch.ch_stream_out,
            ch.ch_stream_in,
            ora.ora_csv_out,
            ora.ora_csv_in,
        )
        for payload in listed:
            if payload.coroutine is None:
                raise AssertionError(f"{payload.name}: body is None")

            self._bodies[payload.name] = payload.coroutine

    async def pg_out(self, statement: str) -> bytes:
        return await self._out("pg_stream_out", statement)

    async def pg_in(self, statement: str, data: bytes, chunk: int | None = None) -> str:
        return await self._in(
            "pg_stream_in", statement, data, chunk, chunk_bytes=self.CHUNK_BYTES
        )

    async def ch_out(self, statement: str) -> bytes:
        return await self._out("ch_stream_out", statement, chunk_bytes=self.CHUNK_BYTES)

    async def ch_in(self, statement: str, data: bytes, chunk: int | None = None) -> str:
        return await self._in(
            "ch_stream_in", statement, data, chunk, chunk_bytes=self.CHUNK_BYTES
        )

    async def ora_out(self, statement: str) -> bytes:
        return await self._out("ora_csv_out", statement)

    async def ora_in(self, table: str, columns: list[str], data: bytes) -> str:
        report = await self._bodies["ora_csv_in"](
            connection=self._required("ora_csv_in"),
            table=table,
            columns=columns,
            feed=Feed(data, self._chunk),
        )

        return report.text

    async def chain(
        self,
        out_name: str,
        out_sql: str,
        in_name: str,
        in_sql: str,
        chunk_bytes: int,
    ) -> Chained:
        """Выход out_name и вход in_name через трубу ОС одновременно; chunk_bytes
        уходит насосам, которые его принимают."""
        pipe = Pipe()
        started = time.monotonic()

        async def produce() -> str:
            try:
                return await self._call(
                    out_name, out_sql, chunk_bytes, out=pipe.outbound
                )
            finally:
                pipe.close_write()

        async def consume() -> str:
            try:
                return await self._call(in_name, in_sql, chunk_bytes, feed=pipe.inbound)
            finally:
                pipe.close_read()

        out_report, in_report = await asyncio.gather(produce(), consume())

        return Chained(out_report, in_report, time.monotonic() - started)

    async def _call(
        self, name: str, statement: str, chunk_bytes: int, **port: Any
    ) -> str:
        extra: dict[str, Any] = {}
        if name != "ora_csv_out":
            extra["chunk_bytes"] = chunk_bytes

        report = await self._bodies[name](
            connection=self._required(name), sql=statement, **port, **extra
        )

        return report.text

    async def _out(self, name: str, statement: str, **extra: Any) -> bytes:
        sink = Sink()
        await self._bodies[name](
            connection=self._required(name), sql=statement, out=sink, **extra
        )

        return sink.data()

    async def _in(
        self,
        name: str,
        statement: str,
        data: bytes,
        chunk: int | None,
        **extra: Any,
    ) -> str:
        if chunk is None:
            chunk = self._chunk

        report = await self._bodies[name](
            connection=self._required(name),
            sql=statement,
            feed=Feed(data, chunk),
            **extra,
        )

        return report.text

    def _required(self, name: str) -> object:
        connection = self._connections[name]
        if connection is None:
            raise AssertionError(f"{name}: the stand has no connection for it")

        return connection
