"""Тела насосов трёх баз, вызванные напрямую с профилями стенда: порты в
памяти или труба ОС между двумя насосами, стейтменты целиком, как их писала
бы LLM."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from boba.db.clickhouse.connection import ClickHouseConfig
from boba.db.oracle.connection import OracleConfig
from boba.db.postgres.connection import CopyOptions, PostgresConfig
from boba.pump_stand.ports import Feed, Pipe, Sink
from boba.tool.ch import tools as ch
from boba.tool.ora import tools as ora
from boba.tool.pg import tools as pg
from boba.toolkit.entry import ToolArgv, ToolMain
from boba.toolkit.ports import PortDirection, StreamPorts
from boba.toolkit.transfer import (
    CreateIfNotExists,
    DeleteNothing,
    InsertFull,
    StreamWire,
)

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
    возвращает тела кадров порта, вход принимает байты и отдаёт текст отчёта;
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
        postgres_target: PostgresConfig | None = None,
    ) -> None:
        """postgres_target — второй сервер postgres для входных насосов;
        без него вход и выход идут в один сервер."""
        self._chunk = chunk
        target = postgres_target
        if target is None:
            target = postgres

        self._connections: dict[str, object | None] = {
            "pg_sync_out": postgres,
            "pg_sync_in": target,
            "ch_stream_out": clickhouse,
            "ch_stream_in": clickhouse,
            "ch_arrow_out": clickhouse,
            "ch_sync_out": clickhouse,
            "ch_sync_in": clickhouse,
            "ch_arrow_in": clickhouse,
            "ora_csv_out": oracle,
            "ora_csv_in": oracle,
            "ora_arrow_out": oracle,
            "ora_sync_out": oracle,
            "ora_arrow_in": oracle,
        }
        self._bodies: dict[str, Body] = {}
        self._ports: dict[str, dict[str, Any]] = {}
        listed = ToolMain.toolset(
            pg.pg_sync_out,
            pg.pg_sync_in,
            ch.ch_stream_out,
            ch.ch_stream_in,
            ch.ch_arrow_out,
            ch.ch_sync_out,
            ch.ch_sync_in,
            ch.ch_arrow_in,
            ora.ora_csv_out,
            ora.ora_csv_in,
            ora.ora_arrow_out,
            ora.ora_sync_out,
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
        """Тела pg_sync_out в раскладке text (COPY text)."""
        return await self._out(
            "pg_sync_out",
            statement,
            wire=StreamWire.TSV,
            copy_options=CopyOptions(chunk_bytes=self.CHUNK_BYTES),
            **extra,
        )

    async def sync_in(self, feed: Feed, **extra: Any) -> str:
        """pg_sync_in из памяти: кадры feed, стратегии по умолчанию — создать
        таблицу, ничего не удалять, вставить всё."""
        arguments: dict[str, Any] = {
            "schema_strategy": CreateIfNotExists(kind="create_if_not_exists"),
            "delete_strategy": DeleteNothing(kind="nothing"),
            "insert_strategy": InsertFull(kind="full"),
        }
        arguments.update(extra)
        report = await self._bodies["pg_sync_in"](
            connection=self._required("pg_sync_in"), feed=feed, **arguments
        )

        return report.text

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

        failures: list[tuple[float, BaseException]] = []

        async def guarded(leg: Coroutine[Any, Any, str]) -> str:
            # оба конца дожидаются друг друга: сорвавшийся конец рвёт трубу, и
            # второй должен успеть закрыть свой дескриптор до следующей трубы
            # теста; наружу идёт та ошибка, что случилась раньше, вторая —
            # её следствие (обрыв трубы, пустой поток)
            try:
                return await leg
            except BaseException as exc:
                failures.append((time.monotonic(), exc))

                return ""

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

        out_report, in_report = await asyncio.gather(
            guarded(produce()), guarded(consume())
        )
        if failures:
            failures.sort(key=lambda item: item[0])

            raise failures[0][1]

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
            feed=Feed(data, chunk, Feed.RAW),
            **extra,
        )

        return report.text

    def _required(self, name: str) -> object:
        connection = self._connections[name]
        if connection is None:
            raise AssertionError(f"{name}: the stand has no connection for it")

        return connection
