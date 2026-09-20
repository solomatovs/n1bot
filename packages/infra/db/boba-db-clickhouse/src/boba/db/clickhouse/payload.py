"""ClickHouse для payload'ов; пула нет — каждый вызов свой процесс и клиент.
Учётные данные приходят через stdin: не видны ни в argv, ни в /proc, ни в логах.

Ошибки:
ClickHouseQueryError — сервер отклонил запрос.
ClickHouseError — до базы не достучаться (сеть, TLS, kerberos, отказ
клиента при инициализации)."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, ClassVar, cast

from clickhouse_connect.driver.asyncclient import AsyncClient
from clickhouse_connect.driver.exceptions import ClickHouseError as DriverError
from clickhouse_connect.driver.query import QueryResult

from boba.db.clickhouse.errors import ClickHouseError, ClickHouseQueryError
from boba.db.clickhouse.profile import ClickHouseConfig
from boba.kerberos import KerberosAuthBase, KerberosError
from boba.krb import ClientCredentials, SpnegoNegotiate

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

    @property
    def service_name(self) -> str:
        return self._service_name

    def copy(self) -> dict[str, str]:
        headers = dict(self)
        headers[self.HEADER] = self._negotiate()
        return headers

    def _negotiate(self) -> str:
        try:
            return SpnegoNegotiate.header(self._service_name)
        except KerberosError as exc:
            msg = (
                f"building Negotiate header for clickhouse service "
                f"{self._service_name} failed: {exc}"
            )
            raise ClickHouseError(msg) from exc


@dataclass(frozen=True, slots=True)
class RowStream:
    names: tuple[str, ...]
    blocks: AsyncIterator[Sequence[Any]]  # тип блока — ваш


# @dataclass(frozen=True)
# class RowBlock:
#     """Поток блоков строк и имена колонок: строки собирает вызывающий.

#     Драйвер отдаёт имена колонок отдельно от значений, поэтому они едут
#     вместе с потоком — иначе каждый вызывающий доставал бы их сам из
#     внутренностей стрима.
#     """

#     names: Sequence[str]
#     block: AsyncIterable[Sequence[Sequence[Any]]]


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
            msg = (
                f"clickhouse {connection.host}:{connection.port}: kerberos "
                f"credentials of {credentials.principal} for service "
                f"{headers.service_name} failed: {type(e).__name__}: {e}"
            )
            raise ClickHouseError(msg) from e

    @staticmethod
    @asynccontextmanager
    async def row_blocks(
        connection: ClickHouseConfig,
        text: str,
        parameters: Mapping[str, Any] | None = None,
    ):
        """Блоки строк запроса; отказ сервера уходит ClickHouseQueryError."""
        values = dict(parameters) if parameters else None
        async with PayloadClickHouse.opened_config(connection) as client:
            try:
                async with await client.query_rows_stream(
                    text, parameters=values
                ) as stream:
                    source = cast(QueryResult, stream.source)
                    yield RowStream(
                        names=tuple(source.column_names),
                        blocks=cast(AsyncIterator, stream),
                    )
            except DriverError as exc:
                raise ClickHouseQueryError(
                    f"query on clickhouse failed: {type(exc).__name__}: {exc}; "
                    f"query: {text[:200]!r}"
                ) from exc

    @staticmethod
    @asynccontextmanager
    async def _client(
        connection: ClickHouseConfig,
        headers: SpnegoHeaders | None,
    ) -> AsyncGenerator[AsyncClient, None]:
        client = PayloadClickHouse._build(connection, headers)
        try:
            await PayloadClickHouse._initialize(client, connection)
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
    async def _initialize(client: AsyncClient, connection: ClickHouseConfig) -> None:
        try:
            await client._initialize()
        except (DriverError, OSError) as e:
            msg = (
                f"connecting to clickhouse {connection.interface}://"
                f"{connection.host}:{connection.port} as {connection.trace()} "
                f"failed: {type(e).__name__}: {e}"
            )
            raise ClickHouseError(msg) from e
