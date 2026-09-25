"""Стороны баз в матрицах перекачки: у каждой — версия, своя схема или база
стенда, создание таблиц, опорная выборка по `order by id` и уборка. Схемы и
базы называются по модулю теста, чтобы модули не мешали друг другу на общем
стенде."""

# ruff: noqa: S608 — стейтменты стенда собираются текстом, как их пишет LLM

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.oracle.payload import PayloadOracle
from boba.db.postgres import AsyncPostgresPool
from boba.pump_stand.compare import FLOAT64_ULP
from boba.pump_stand.oracle import OracleStand, PumpUser
from boba.pump_stand.stand import ChSource, OraSource, PgSource

__all__ = ["ClickHouseSide", "OracleSide", "PostgresSide"]


class PostgresSide:
    """PostgreSQL или Greenplum стенда: версия, схема под тесты, опорная
    выборка в той же зафиксированной сессии COPY, что у насосов."""

    GREENPLUM_6: ClassVar[str] = "Greenplum Database 6"

    def __init__(self, source: PgSource, schema: str) -> None:
        self.source = source
        self.schema = schema
        self.version = 0
        self.greenplum_6 = False

    @property
    def profile(self) -> Any:
        return self.source.postgres

    async def connect(self) -> None:
        async with await AsyncPostgresPool.dedicated(self.source.postgres) as conn:
            cursor = await conn.execute(
                "select current_setting('server_version_num'), version()"
            )
            row = await cursor.fetchone()
            if row is None:
                raise AssertionError("version() returned no row")

        self.version = int(row[0])
        self.greenplum_6 = self.GREENPLUM_6 in row[1]

    def double_tolerance(self) -> float:
        """Greenplum 6 разбирает текст float8 своим strtod и для части значений
        ошибается на одну ULP (1.942e-297 -> 1.9419999999999998e-297), тогда
        как PostgreSQL 9.4 той же основы точен; остальные серверы — бит в бит."""
        if self.greenplum_6:
            return FLOAT64_ULP

        return 0.0

    async def recreate_schema(self, statements: Sequence[str] = ()) -> None:
        """Пустая схема тестов и её типы; search_path на неё."""
        async with await AsyncPostgresPool.dedicated(self.source.postgres) as conn:
            await conn.execute(self._q(f"drop schema if exists {self.schema} cascade"))
            await conn.execute(self._q(f"create schema {self.schema}"))
            await conn.execute(self._q(f"set search_path to {self.schema}, public"))
            for statement in statements:
                await conn.execute(self._q(statement))

    async def execute(self, statements: Sequence[str]) -> None:
        async with await AsyncPostgresPool.dedicated(self.source.postgres) as conn:
            await conn.execute(self._q(f"set search_path to {self.schema}, public"))
            for statement in statements:
                await conn.execute(self._q(statement))

    async def create(self, table: str, columns: Sequence[str]) -> None:
        await self.execute(
            (
                f"drop table if exists {self.schema}.{table}",
                f"create table {self.schema}.{table} ({', '.join(columns)})",
            )
        )

    async def select(self, table: str, expressions: Sequence[str]) -> list[Any]:
        """Опорная выборка в сессии COPY насосов: иначе money и float печатались
        бы по настройкам базы."""
        profile = self.source.postgres.copy_text()
        async with await AsyncPostgresPool.dedicated(profile) as conn:
            await conn.execute(self._q(f"set search_path to {self.schema}, public"))
            cursor = await conn.execute(
                self._q(
                    f"select {', '.join(expressions)} from {self.schema}.{table} "
                    "order by id"
                )
            )
            return list(await cursor.fetchall())

    async def drop(self) -> None:
        async with await AsyncPostgresPool.dedicated(self.source.postgres) as conn:
            await conn.execute(self._q(f"drop schema if exists {self.schema} cascade"))

    @staticmethod
    def _q(text: str) -> bytes:
        """psycopg принимает литеральную строку или bytes; текст собран."""
        return text.encode()


class ClickHouseSide:
    """ClickHouse стенда: мажорная версия, precise_float_parsing, база тестов."""

    def __init__(self, source: ChSource, database: str) -> None:
        self.source = source
        self.database = database
        self.major = 0
        self.precise_floats = False

    @property
    def profile(self) -> Any:
        return self.source.admin

    async def connect(self) -> None:
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            version = await client.query("select version()")
            found = await client.query(
                "select count() from system.settings "
                "where name = 'precise_float_parsing'"
            )

        release, *_ = str(version.result_rows[0][0]).split(".")
        self.major = int(release)
        self.precise_floats = found.result_rows[0][0] == 1

    async def recreate_database(self) -> None:
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            await client.command(f"drop database if exists {self.database}")
            await client.command(f"create database {self.database}")

    async def command(self, text: str, settings: Mapping[str, Any] = {}) -> None:
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            await client.command(text, settings=dict(settings))

    async def create(self, table: str, columns: Sequence[str]) -> None:
        await self.command(f"drop table if exists {self.database}.{table}")
        await self.command(
            f"create table {self.database}.{table} ({', '.join(columns)}) "
            "engine = MergeTree order by id"
        )

    async def select(self, table: str, expressions: Sequence[str]) -> list[Any]:
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            result = await client.query(
                f"select {', '.join(expressions)} from {self.database}.{table} "
                "order by id"
            )

        return list(result.result_rows)

    async def drop(self) -> None:
        await self.command(f"drop database if exists {self.database}")


class OracleSide:
    """Oracle стенда: версия и кодировка, схема PUMP_STAND, таблицы владельца."""

    def __init__(self, source: OraSource, arraysize: int) -> None:
        self.source = source
        self.stand = OracleStand(source)
        self.arraysize = arraysize
        self.version = 0
        self.unicode = False

    @property
    def profile(self) -> Any:
        """Профиль владельца схемы с пачкой выгрузки arraysize."""
        return self.stand.owner.model_copy(update={"arraysize": self.arraysize})

    async def connect(self) -> None:
        self.version = await self.stand.version()
        payload = PayloadOracle(self.source.admin)
        async with (
            payload.opened() as conn,
            payload.rows(
                conn,
                "select value from nls_database_parameters "
                "where parameter = 'NLS_CHARACTERSET'",
            ) as stream,
        ):
            rows = [row async for row in stream.blocks]

        self.unicode = rows[0][0] == "AL32UTF8"

    async def recreate_user(self) -> None:
        await self.stand.recreate_user()

    async def run(
        self, statements: Sequence[str], parameters: Mapping[str, object] | None = None
    ) -> None:
        await self.stand.run(statements, parameters)

    async def create(self, table: str, columns: Sequence[str]) -> None:
        await self.stand.run((f"create table {table} ({', '.join(columns)})",))

    async def drop_table(self, table: str) -> None:
        await self.stand.run((f"drop table {table} purge",))

    async def select(self, table: str, expressions: Sequence[str]) -> list[Any]:
        payload = PayloadOracle(self.stand.owner)
        async with (
            payload.opened() as conn,
            payload.rows(
                conn,
                f"select {', '.join(expressions)} from {PumpUser.NAME}.{table} "
                "order by id",
            ) as stream,
        ):
            return [row async for row in stream.blocks]

    async def drop(self) -> None:
        await self.stand.drop()
