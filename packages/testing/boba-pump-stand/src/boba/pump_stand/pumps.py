"""Тела насосов трёх баз, вызванные напрямую с профилями стенда: порты в
памяти или труба ОС между двумя насосами, стейтменты целиком, как их писала
бы LLM."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from boba.db.clickhouse.connection import ClickHouseConfig
from boba.db.oracle.connection import OracleConfig
from boba.db.postgres.connection import PostgresConfig
from boba.pump_stand.ports import Feed, Pipe, Sink
from boba.tool.ch import tools as ch
from boba.tool.ora import tools as ora
from boba.tool.pg import tools as pg
from boba.toolkit.entry import ToolArgv, ToolMain
from boba.toolkit.ports import PortDirection, StreamPorts

__all__ = ["Chained", "Leg", "Pumps"]

Body = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class Leg:
    """Конец цепочки: имя насоса и его аргументы, кроме соединения и порта."""

    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class Chained:
    """Итог цепочки двух насосов через трубу: отчёты обоих и время целиком."""

    out_report: str
    in_report: str
    seconds: float


class Pumps:
    """Насосы postgres, ClickHouse и Oracle над профилями стенда. Выход
    возвращает байты порта, вход принимает байты и отдаёт текст отчёта;
    extra — остальные аргументы фасада (before, after, session);
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
            "pg_arrow_out": postgres,
            "pg_arrow_in": postgres,
            "ch_stream_out": clickhouse,
            "ch_stream_in": clickhouse,
            "ch_arrow_out": clickhouse,
            "ch_arrow_in": clickhouse,
            "ora_csv_out": oracle,
            "ora_csv_in": oracle,
            "ora_arrow_out": oracle,
            "ora_arrow_in": oracle,
        }
        self._bodies: dict[str, Body] = {}
        self._ports: dict[str, dict[str, Any]] = {}
        listed = ToolMain.toolset(
            pg.pg_stream_out,
            pg.pg_stream_in,
            pg.pg_arrow_out,
            pg.pg_arrow_in,
            ch.ch_stream_out,
            ch.ch_stream_in,
            ch.ch_arrow_out,
            ch.ch_arrow_in,
            ora.ora_csv_out,
            ora.ora_csv_in,
            ora.ora_arrow_out,
            ora.ora_arrow_in,
        )
        for payload in listed:
            if payload.coroutine is None:
                raise AssertionError(f"{payload.name}: body is None")

            self._bodies[payload.name] = payload.coroutine
            self._ports[payload.name] = ToolArgv.port_fields(
                ToolArgv.schema_of(payload)
            )

    async def pg_out(self, statement: str, **extra: Any) -> bytes:
        return await self._out("pg_stream_out", statement, **extra)

    async def pg_in(
        self, statement: str, data: bytes, chunk: int | None = None, **extra: Any
    ) -> str:
        return await self._in(
            "pg_stream_in",
            statement,
            data,
            chunk,
            chunk_bytes=self.CHUNK_BYTES,
            **extra,
        )

    async def ch_out(self, statement: str, **extra: Any) -> bytes:
        return await self._out(
            "ch_stream_out", statement, chunk_bytes=self.CHUNK_BYTES, **extra
        )

    async def ch_in(
        self, statement: str, data: bytes, chunk: int | None = None, **extra: Any
    ) -> str:
        return await self._in(
            "ch_stream_in",
            statement,
            data,
            chunk,
            chunk_bytes=self.CHUNK_BYTES,
            **extra,
        )

    async def ora_out(self, statement: str, **extra: Any) -> bytes:
        return await self._out("ora_csv_out", statement, **extra)

    async def ora_in(self, statement: str, data: bytes, **extra: Any) -> str:
        return await self._in(
            "ora_csv_in", statement, data, None, chunk_bytes=self.CHUNK_BYTES, **extra
        )

    async def chain(self, out: Leg, into: Leg) -> Chained:
        """Выход out и вход into через трубу ОС одновременно."""
        pipe = Pipe(
            self._port(out.name, PortDirection.OUTBOUND),
            self._port(into.name, PortDirection.INBOUND),
        )
        started = time.monotonic()

        async def produce() -> str:
            try:
                return await self._call(out, out=pipe.outbound)
            finally:
                pipe.close_write()

        async def consume() -> str:
            try:
                return await self._call(into, feed=pipe.inbound)
            finally:
                pipe.close_read()

        out_report, in_report = await asyncio.gather(produce(), consume())

        return Chained(out_report, in_report, time.monotonic() - started)

    def _port(self, name: str, direction: PortDirection) -> Any:
        """Класс порта тела в направлении direction: как объявлен в подписи."""
        for annotation in self._ports[name].values():
            if StreamPorts.direction_of(annotation) is direction:
                return annotation

        raise AssertionError(f"{name}: no {direction} port declared")

    async def _call(self, leg: Leg, **port: Any) -> str:
        report = await self._bodies[leg.name](
            connection=self._required(leg.name), **leg.arguments, **port
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
