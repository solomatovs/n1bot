"""Проверка соединения по профилю: открыть, выполнить пробный запрос, закрыть.

Ошибки: своих не выпускает — исход любой проверки описывает ProbeResult.
"""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

import httpx
from psycopg import sql
from pydantic import BaseModel, ConfigDict

from boba.connection_broker.tickets import DelegationSource, TicketArming
from boba.connections.clickhouse import ClickHouseConfig
from boba.connections.http import HttpProfile
from boba.connections.postgres import PostgresConfig
from boba.connections.profile import ConnectionProfile
from boba.db.clickhouse.errors import ClickHouseError
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.postgres.async_pool import PostgresError
from boba.db.postgres.payload import PayloadPostgres
from boba.identity.errors import RefusalError
from boba.krb import KerberosError
from boba.toolkit.timing import Elapsed
from boba.toolrun.injected import ToolConfigError
from boba.transport.http import HttpRequest, HttpTransport

__all__ = ["ConnectionProbe", "ProbeResult"]

logger = logging.getLogger(__name__)


class ProbeResult(BaseModel):
    """Исход проверки: удалось ли открыть соединение, что ответил сервер, за сколько."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    message: str
    elapsed_ms: int


class ConnectionProbe:
    """Пробное соединение по профилю с билетом вызова вместо kerberos-секции."""

    TIMEOUT_SEC: ClassVar[float] = 15.0
    PROBE_SQL: ClassVar[sql.SQL] = sql.SQL("select version()")

    def __init__(self, delegation: DelegationSource) -> None:
        self._arming = TicketArming(delegation)

    async def probe(self, profile: ConnectionProfile) -> ProbeResult:
        elapsed = Elapsed()
        try:
            armed = await self._arming.arm_profile(profile)
            message = await asyncio.wait_for(self._open(armed), self.TIMEOUT_SEC)
        except TimeoutError:
            return ProbeResult(
                ok=False,
                message=f"no answer in {self.TIMEOUT_SEC:.0f}s",
                elapsed_ms=elapsed.ms(),
            )
        except (
            PostgresError,
            ClickHouseError,
            KerberosError,
            ToolConfigError,
            RefusalError,
            httpx.HTTPError,
            OSError,
        ) as exc:
            logger.info("connection probe failed: %s", exc)
            return ProbeResult(ok=False, message=str(exc), elapsed_ms=elapsed.ms())

        return ProbeResult(ok=True, message=message, elapsed_ms=elapsed.ms())

    async def _open(self, profile: ConnectionProfile) -> str:
        if isinstance(profile, PostgresConfig):
            return await self._postgres(profile)

        if isinstance(profile, ClickHouseConfig):
            return await self._clickhouse(profile)

        return await self._web(profile)

    async def _postgres(self, profile: PostgresConfig) -> str:
        conn = await PayloadPostgres.connect_config(profile)
        try:
            async with conn.cursor() as cur:
                await cur.execute(self.PROBE_SQL)
                row = await cur.fetchone()
        finally:
            await conn.close()

        return self._first(row)

    async def _clickhouse(self, profile: ClickHouseConfig) -> str:
        async with PayloadClickHouse.opened_config(profile) as client:
            result = await client.query(self.PROBE_SQL.as_string())

        rows = result.result_rows
        if not rows:
            return "connected"

        return self._first(rows[0])

    async def _web(self, profile: HttpProfile) -> str:
        if not profile.base_url:
            msg = "base_url is required to check a web connection"
            raise ToolConfigError(msg)

        async with (
            HttpTransport(profile) as transport,
            transport.fetch(HttpRequest(url=profile.base_url)) as got,
        ):
            await got.stream.read()
            return f"HTTP {got.status}"

    @staticmethod
    def _first(row: object) -> str:
        if isinstance(row, tuple | list) and row:
            return str(row[0])

        return "connected"
