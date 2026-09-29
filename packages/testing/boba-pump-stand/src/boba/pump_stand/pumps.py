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
from boba.toolkit.ports import PortDirection, StreamGroup, StreamPorts
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
    """Насосы postgres, ClickHouse и Oracle над профилями стенда. pg_out
    возвращает тела кадров порта, sync_in принимает кадры из памяти и отдаёт
    текст отчёта; extra — остальные аргументы фасада (before, after,
    copy_options); chain соединяет два насоса трубой и гонит их одновременно."""

    CHUNK_BYTES: ClassVar[int] = 4096
    """Размер порции насосов с chunk_bytes: нижняя граница фасада."""

    def __init__(
        self,
        postgres: PostgresConfig | None = None,
        clickhouse: ClickHouseConfig | None = None,
        oracle: OracleConfig | None = None,
        postgres_target: PostgresConfig | None = None,
    ) -> None:
        """postgres_target — второй сервер postgres для входных насосов;
        без него вход и выход идут в один сервер."""
        target = postgres_target
        if target is None:
            target = postgres

        self._connections: dict[str, object | None] = {
            "pg_stream_out": postgres,
            "pg_stream_in": target,
            "ch_stream_out": clickhouse,
            "ch_stream_in": clickhouse,
            "ora_stream_out": oracle,
            "ora_stream_in": oracle,
        }
        self._bodies: dict[str, Body] = {}
        self._ports: dict[str, dict[str, Any]] = {}
        self._groups: dict[str, tuple[str, ...]] = {}
        listed = ToolMain.toolset(
            pg.pg_stream_out,
            pg.pg_stream_in,
            ch.ch_stream_out,
            ch.ch_stream_in,
            ora.ora_stream_out,
            ora.ora_stream_in,
        )
        for payload in listed:
            if payload.coroutine is None:
                raise AssertionError(f"{payload.name}: body is None")

            self._bodies[payload.name] = payload.coroutine
            self._ports[payload.name] = ToolArgv.port_fields(
                ToolArgv.schema_of(payload)
            )
            self._groups[payload.name] = ToolArgv.group_fields(
                ToolArgv.schema_of(payload)
            )

    async def pg_out(self, statement: str, **extra: Any) -> bytes:
        """Тела pg_stream_out в раскладке text (COPY text)."""
        return await self._out(
            "pg_stream_out",
            statement,
            wire=StreamWire.TSV,
            copy_options=CopyOptions(chunk_bytes=self.CHUNK_BYTES),
            **extra,
        )

    async def sync_in(self, feed: Feed, **extra: Any) -> str:
        """pg_stream_in из памяти: кадры feed, стратегии по умолчанию — создать
        таблицу, ничего не удалять, вставить всё."""
        arguments: dict[str, Any] = {
            "schema_strategy": CreateIfNotExists(kind="create_if_not_exists"),
            "delete_strategy": DeleteNothing(kind="nothing"),
            "insert_strategy": InsertFull(kind="full"),
        }
        arguments.update(extra)
        report = await self._bodies["pg_stream_in"](
            connection=self._required("pg_stream_in"),
            feed=feed,
            **self._detached_groups("pg_stream_in"),
            **arguments,
        )

        return report.text

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
            connection=self._required(leg.name),
            **leg.arguments,
            **port,
            **self._detached_groups(leg.name),
        )

        return report.text

    def _detached_groups(self, name: str) -> dict[str, StreamGroup]:
        """Барьер группы вне группы: ready() возвращается сразу, как у
        вызова человеком."""
        groups: dict[str, StreamGroup] = {}
        for field in self._groups[name]:
            groups[field] = StreamGroup(-1, -1)

        return groups

    async def _out(self, name: str, statement: str, **extra: Any) -> bytes:
        sink = Sink()
        await self._bodies[name](
            connection=self._required(name), sql=statement, out=sink, **extra
        )

        return sink.data()

    def _required(self, name: str) -> object:
        connection = self._connections[name]
        if connection is None:
            raise AssertionError(f"{name}: the stand has no connection for it")

        return connection
