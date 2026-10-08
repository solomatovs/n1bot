"""Тела насосов, вызванные напрямую с профилями стенда на портах в памяти:
проверки самих кадров потока. Пары насосов стенд гоняет описаниями DAG
(boba.pump_stand.dags)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from boba.db.clickhouse.connection import ClickHouseConfig
from boba.db.oracle.connection import OracleConfig
from boba.db.postgres.connection import PostgresConfig
from boba.pump_stand.ports import Feed, Sink
from boba.tool.ch import tools as ch
from boba.tool.ora import tools as ora
from boba.tool.pg import tools as pg
from boba.toolkit.entry import ToolArgv, ToolMain
from boba.toolkit.ports import StreamGroup, ToolProgress
from boba.toolkit.result import FailureResult
from boba.toolkit.transfer import (
    CommandsFailedError,
    CreateIfNotExists,
    DeleteNothing,
    InsertFull,
    StreamWire,
)

__all__ = ["Pumps"]

Body = Callable[..., Awaitable[Any]]


class Pumps:
    """Насосы postgres, ClickHouse и Oracle над профилями стенда. pg_out
    возвращает тела кадров порта, sync_in принимает кадры из памяти и отдаёт
    текст отчёта; extra — остальные аргументы фасада (before, after,
    copy_options)."""

    def __init__(
        self,
        postgres: PostgresConfig | None = None,
        clickhouse: ClickHouseConfig | None = None,
        oracle: OracleConfig | None = None,
        postgres_target: PostgresConfig | None = None,
        progress: ToolProgress | None = None,
    ) -> None:
        """postgres_target — второй сервер postgres для входных насосов;
        без него вход и выход идут в один сервер. progress — куда тела пишут
        отчёты о ходе работы; None — отчёты никуда не уходят."""
        heard = progress
        if heard is None:
            heard = ToolProgress(-1)

        self._progress_heard = heard
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
        self._failed: dict[str, CommandsFailedError] = {}
        self._ports: dict[str, dict[str, Any]] = {}
        self._groups: dict[str, tuple[str, ...]] = {}
        self._progress: dict[str, tuple[str, ...]] = {}
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
            self._progress[payload.name] = ToolArgv.progress_fields(
                ToolArgv.schema_of(payload)
            )

    async def pg_out(self, statement: str, **extra: Any) -> bytes:
        """Тела pg_stream_out в раскладке text (COPY text)."""
        return await self._out(
            "pg_stream_out",
            statement,
            wire=StreamWire.TSV,
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
        report = await self._invoked(
            "pg_stream_in",
            self._bodies["pg_stream_in"](
                connection=self._required("pg_stream_in"),
                feed=feed,
                **self._detached("pg_stream_in"),
                **arguments,
            ),
        )

        return report.llm_view()

    def _detached(self, name: str) -> dict[str, StreamGroup | ToolProgress]:
        """Служебные параметры тела вне группы и без слушателя, как у вызова
        человеком: барьер группы, чей ready() возвращается сразу, и отчёты
        о ходе работы приёмнику стенда."""
        built: dict[str, StreamGroup | ToolProgress] = {}
        for field in self._groups[name]:
            built[field] = StreamGroup(-1, -1)

        for field in self._progress[name]:
            built[field] = self._progress_heard

        return built

    async def _out(self, name: str, statement: str, **extra: Any) -> bytes:
        sink = Sink()
        await self._invoked(
            name,
            self._bodies[name](
                connection=self._required(name),
                sql=statement,
                out=sink,
                **self._detached(name),
                **extra,
            ),
        )

        return sink.data()

    def failure_of(self, name: str) -> FailureResult:
        """Вид последнего сбоя насоса name, каким его увидит чат: ошибка,
        колонки и выполненные команды."""
        failed = self._failed.get(name)
        if failed is None:
            raise AssertionError(f"{name}: the pump has not failed")

        return failed.failure()

    async def _invoked(self, name: str, body: Awaitable[Any]) -> Any:
        """Тело насоса; его сбой — исходная доменная ошибка: насос оборачивает
        её в CommandsFailedError ради вида в чате, стенд проверяет сам отказ,
        а вид отдаёт failure_of."""
        try:
            return await body
        except CommandsFailedError as exc:
            self._failed[name] = exc
            failed = exc

        raise failed.cause

    def _required(self, name: str) -> object:
        connection = self._connections[name]
        if connection is None:
            raise AssertionError(f"{name}: the stand has no connection for it")

        return connection
