"""ClickHouse для payload'ов; пула нет — каждый вызов свой процесс и клиент.
Учётные данные приходят через stdin: не видны ни в argv, ни в /proc, ни в логах.

Ошибки: ClickHouseError — до базы не достучаться (сеть, TLS, kerberos, отказ
клиента при инициализации)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import ClassVar

from clickhouse_connect.driver.asyncclient import AsyncClient
from clickhouse_connect.driver.exceptions import ClickHouseError as DriverError

from boba.db.clickhouse.config import ClickHouseConfig
from boba.db.clickhouse.errors import ClickHouseError
from boba.krb import (
    ClientCredentials,
    KerberosAuthBase,
    KerberosError,
    SpnegoNegotiate,
)

__all__ = ["PayloadClickHouse", "SpnegoHeaders"]


class SpnegoHeaders(dict[str, str]):
    """Заголовки клиента со свежим Negotiate на каждый HTTP-запрос.

    ClickHouse отвергает повторно присланный AP-REQ как replay, поэтому один
    заголовок на весь клиент не работает: со второго запроса сервер перестаёт
    видеть принципала. clickhouse-connect снимает copy() заголовков перед
    каждым запросом — токен и выпускается здесь, в copy().

    Токен строится по кредам из окружения процесса (KRB5CCNAME/KRB5_CONFIG),
    поэтому клиент живёт внутри KerberosCredentials.applied_async().
    """

    HEADER: ClassVar[str] = SpnegoNegotiate.HEADER

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service_name = service_name

    def copy(self) -> dict[str, str]:
        headers = dict(self)
        headers[self.HEADER] = self._negotiate()
        return headers

    def _negotiate(self) -> str:
        try:
            return SpnegoNegotiate.header(self._service_name)
        except KerberosError as exc:
            raise ClickHouseError(str(exc)) from exc


class PayloadClickHouse:
    """Клиент по параметрам запроса; строки приводит SqlRows вызывающей стороны."""

    @staticmethod
    @asynccontextmanager
    async def opened_config(
        connection: ClickHouseConfig,
    ) -> AsyncGenerator[AsyncClient, None]:
        """Клиент на время операции; kerberos-окружение держится всё это время."""
        if not isinstance(connection.auth, KerberosAuthBase):
            async with PayloadClickHouse._client(connection, None) as client:
                yield client
            return

        credentials = ClientCredentials.of(connection.auth)
        headers = SpnegoHeaders(connection.service_name())
        try:
            async with (
                credentials.applied_async(),
                PayloadClickHouse._client(connection, headers) as client,
            ):
                yield client
        except KerberosError as e:
            msg = f"kerberos failed: {type(e).__name__}: {e}"
            raise ClickHouseError(msg) from e

    @staticmethod
    @asynccontextmanager
    async def _client(
        connection: ClickHouseConfig,
        headers: SpnegoHeaders | None,
    ) -> AsyncGenerator[AsyncClient, None]:
        client = PayloadClickHouse._build(connection, headers)
        try:
            await PayloadClickHouse._initialize(client)
            yield client
        finally:
            await client.close()

    @staticmethod
    def _build(
        connection: ClickHouseConfig,
        headers: SpnegoHeaders | None,
    ) -> AsyncClient:
        """Клиент собирается вручную: живые заголовки нужны уже на инициализации.

        clickhouse_connect.get_async_client() сам ходит в сервер тремя запросами,
        а заголовки складывает в обычный dict — подменить их после этого поздно.
        """
        client = AsyncClient(
            autogenerate_session_id=False,
            **connection.client_settings(),
        )
        if headers is not None:
            headers.update(client.headers)
            client.headers = headers
            client._backend.headers = headers
        return client

    @staticmethod
    async def _initialize(client: AsyncClient) -> None:
        try:
            await client._initialize()
        except (DriverError, OSError) as e:
            msg = f"connect failed: {type(e).__name__}: {e}"
            raise ClickHouseError(msg) from e
