"""ClickHouse для payload'ов; пула нет — каждый вызов свой процесс и клиент.
Учётные данные приходят через stdin: не видны ни в argv, ни в /proc, ни в логах.

Ошибки:
ClickHouseQueryError — сервер отклонил запрос или чтению заданы размеры,
    которые не применить.
ClickHouseError — до базы не достучаться (сеть, TLS, kerberos, отказ
клиента при инициализации)."""

from __future__ import annotations

import json
from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    AsyncIterator,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import tzinfo
from typing import Any, Protocol, cast, runtime_checkable

import aiohttp
from clickhouse_connect.datatypes.base import ClickHouseType
from clickhouse_connect.driver._backend.http_async import release_lease
from clickhouse_connect.driver.asyncclient import AsyncClient
from clickhouse_connect.driver.binding import bind_query
from clickhouse_connect.driver.exceptions import ClickHouseError as DriverError
from clickhouse_connect.driver.external import ExternalData
from clickhouse_connect.driver.query import QueryContext, QueryResult, TzMode
from clickhouse_connect.driver.summary import QuerySummary

from boba.db.clickhouse.connection import ClickHouseConfig, SpnegoHeaders
from boba.db.clickhouse.errors import ClickHouseError, ClickHouseQueryError
from boba.db.clickhouse.trace import ChHeader, ChQueryTrace

__all__ = [
    "ByteStream",
    "PayloadClickHouse",
    "ReadTuning",
    "RowStream",
]


@dataclass(frozen=True, slots=True)
class RowStream:
    """
    Поток строк запроса в python объектах
    """

    names: tuple[str, ...]
    blocks: AsyncIterator[Sequence[Any]]
    column_oriented: bool
    column_types: tuple[ClickHouseType, ...]
    query_id: str
    summary: Mapping[str, Any]


@dataclass(frozen=True)
class ByteStream:
    """Ответ запроса сырыми байтами HTTP-протокола ClickHouse в формате, который
    выбрал вызывающий: блоки идут как пришли, без разбора. Форматеры из
    boba.db.clickhouse.formats оборачивают его в поток своего формата. trace —
    сводка из заголовков ответа."""

    blocks: AsyncIterator[memoryview]
    trace: ChQueryTrace


@runtime_checkable
class SocketReadSize(Protocol):
    """Транспорт селекторного цикла asyncio: читает сокет порциями max_size.
    У транспорта uvloop такого атрибута нет."""

    max_size: int


@dataclass(frozen=True)
class SocketReadRestore:
    """Прежний размер чтения сокета транспорта: соединение уходит обратно в
    пул aiohttp, и следующий запрос не должен унаследовать чужой размер."""

    transport: SocketReadSize
    size: int

    def restore(self) -> None:
        self.transport.max_size = self.size


@dataclass(frozen=True)
class ReadTuning:
    """Рычаги размера блоков при чтении ответа, на один запрос. Драйвер их не
    открывает, поэтому ставятся во внутренние поля aiohttp и asyncio уже
    созданного ответа; первые байты ответа успевают прийти с прежними
    размерами.

    socket_read_size — сколько байт транспорт asyncio читает из сокета за
    раз; пока потребитель успевает, блок потока равен одному такому чтению.
    По умолчанию 256 КиБ. Меняется у транспорта этого соединения и
    возвращается при закрытии потока; есть только у селекторного цикла
    asyncio, под uvloop вызов отклоняется.

    read_buffer_size — нижняя граница буфера ответа aiohttp, верхняя вдвое
    больше. Когда потребитель отстаёт, накопленное отдаётся одним блоком, а
    за верхней границей aiohttp перестаёт читать сокет; поэтому самый большой
    блок — верхняя граница плюс одно чтение сокета. По умолчанию 256 КиБ.
    """

    socket_read_size: int | None = None
    read_buffer_size: int | None = None

    def __post_init__(self) -> None:
        self._positive("socket_read_size", self.socket_read_size)
        self._positive("read_buffer_size", self.read_buffer_size)

    def applied(self, response: aiohttp.ClientResponse) -> SocketReadRestore | None:
        """Ставит рычаги ответу; возвращает то, что вернуть транспорту при
        закрытии потока, или None."""
        if size := self.read_buffer_size:
            response.content._low_water = size
            response.content._high_water = size * 2

        size = self.socket_read_size
        if size is None:
            return None

        connection = response.connection
        if connection is None:
            raise ClickHouseQueryError(
                "read tuning: socket_read_size expects an open connection of the "
                "response, the response already released it"
            )

        transport = connection.transport
        if not isinstance(transport, SocketReadSize):
            raise ClickHouseQueryError(
                f"read tuning: socket_read_size expects an asyncio selector "
                f"transport with max_size, got {type(transport).__name__}"
            )

        restore = SocketReadRestore(transport, transport.max_size)
        transport.max_size = size

        return restore

    def _positive(self, name: str, value: int | None) -> None:
        if value is None:
            return

        if value < 1:
            raise ClickHouseQueryError(
                f"read tuning: {name} expects a positive number of bytes, got {value}"
            )


class PayloadClickHouse:
    """Исполнитель запросов ClickHouse: принимает готовый текст, параметры и
    настройки драйвера как есть и ничего не знает ни о том, как их собрали,
    ни о форматах потоков — их разбирают форматеры boba.db.clickhouse.formats
    поверх byte_stream_out и byte_stream_in."""

    @staticmethod
    @asynccontextmanager
    async def opened_config(
        connection: ClickHouseConfig,
    ) -> AsyncGenerator[AsyncClient, None]:
        """Клиент на время операции; окружение авторизации держит
        ClickHouseAuthSession профиля всё это время."""
        session = connection.auth_session()
        async with (
            session.applied() as headers,
            PayloadClickHouse._client(connection, headers) as client,
        ):
            yield client

    @staticmethod
    @asynccontextmanager
    async def rows_stream_out(  # noqa: PLR0913
        client: AsyncClient,
        text: str,
        parameters: Mapping[str, Any] | Sequence[Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        query_formats: Mapping[str, str] | None = None,
        column_formats: Mapping[str, str | dict[str, str]] | None = None,
        encoding: str | None = None,
        use_none: bool | None = None,
        context: QueryContext | None = None,
        query_tz: str | tzinfo | None = None,
        column_tzs: Mapping[str, str | tzinfo] | None = None,
        external_data: ExternalData | None = None,
        transport_settings: Mapping[str, str] | None = None,
        tz_mode: TzMode | None = None,
    ) -> AsyncGenerator[RowStream, None]:
        """
        Возращает поток rows в виде python объектов.
        Все аргументы после text — аргументы query_rows_stream драйвера,
        передаются ему как есть.
        """
        try:
            async with await client.query_rows_stream(
                text,
                parameters=PayloadClickHouse._params(parameters),
                settings=PayloadClickHouse._dict(settings),
                query_formats=PayloadClickHouse._dict(query_formats),
                column_formats=PayloadClickHouse._dict(column_formats),
                encoding=encoding,
                use_none=use_none,
                context=context,
                query_tz=query_tz,
                column_tzs=PayloadClickHouse._dict(column_tzs),
                external_data=external_data,
                transport_settings=PayloadClickHouse._dict(transport_settings),
                tz_mode=tz_mode,
            ) as stream:
                source = cast(QueryResult, stream.source)
                names = tuple(source.column_names)

                yield RowStream(
                    names=names,
                    blocks=cast(AsyncIterator[Sequence[Any]], stream),
                    column_oriented=source.column_oriented,
                    column_types=tuple(source.column_types),
                    query_id=source.query_id,
                    summary=source.summary,
                )
        except DriverError as exc:
            raise ClickHouseQueryError(
                f"query on clickhouse failed: {type(exc).__name__}: {exc}; "
                f"query: {text[:200]!r}"
            ) from exc

    @staticmethod
    @asynccontextmanager
    async def byte_stream_out(  # noqa: PLR0913
        client: AsyncClient,
        query: str,
        fmt: str | None = None,
        parameters: Mapping[str, Any] | Sequence[Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        use_database: bool = True,
        external_data: ExternalData | None = None,
        transport_settings: Mapping[str, str] | None = None,
        tuning: ReadTuning | None = None,
    ) -> AsyncGenerator[ByteStream, None]:
        """
        Выполняет запрос и отдаёт сырой http-поток ответа блоками, без
        какого-либо разбора и преобразования в python объекты. fmt драйвер
        дописывает к запросу как FORMAT; без fmt в силе FORMAT из текста
        запроса, а без него сервер отвечает TabSeparated.

        Аргументы от parameters до transport_settings — аргументы raw_stream
        драйвера, передаются ему как есть. tuning задаёт размер блоков
        (ReadTuning); чтобы до него добраться, повторены шаги raw_stream
        драйвера, которые прячут объект ответа aiohttp.
        """
        restore: SocketReadRestore | None = None
        try:
            final_query, bind_params, runtime = client._prep_raw_query_runtime(
                query,
                PayloadClickHouse._params(parameters),
                PayloadClickHouse._dict(settings),
                fmt,
                use_database,
            )
            response = await client._backend.execute_raw_stream(
                final_query,
                bind_params,
                external_data,
                runtime,
                PayloadClickHouse._dict(transport_settings),
            )
        except DriverError as exc:
            raise ClickHouseQueryError(
                f"query on clickhouse in format {fmt} failed: "
                f"{type(exc).__name__}: {exc}; query: {query[:200]!r}"
            ) from exc

        async def blocks(
            content: aiohttp.StreamReader,
        ) -> AsyncIterator[memoryview]:
            async for chunk in content.iter_any():
                yield memoryview(chunk)

        try:
            if tuning is not None:
                restore = tuning.applied(response)

            yield ByteStream(
                blocks=blocks(response.content),
                trace=PayloadClickHouse._trace_of_headers(response.headers),
            )
        except (DriverError, aiohttp.ClientError) as exc:
            raise ClickHouseQueryError(
                f"reading clickhouse response in format {fmt} failed: "
                f"{type(exc).__name__}: {exc}; query: {query[:200]!r}"
            ) from exc
        finally:
            if restore is not None:
                restore.restore()

            try:
                response.close()
            finally:
                release_lease(response)

    @staticmethod
    async def byte_stream_in(  # noqa: PLR0913
        client: AsyncClient,
        query: str,
        parameters: Mapping[str, Any] | Sequence[Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        transport_settings: Mapping[str, str] | None = None,
        *,
        blocks: AsyncIterable[bytes | bytearray | memoryview],
    ) -> ChQueryTrace:
        """
        Позволяет выполнить запрос на INSERT ... FORMAT
        В качестве потока на вход может быть передан любой итератор
        возвращающий blocks: AsyncIterable[bytes | bytearray | memoryview]
        однако нужно что бы он был совместим с указанных в FORMAT аргументе
        """
        values = PayloadClickHouse._params(parameters)
        text, server_params = bind_query(query, values, client.server_tz)
        if server_params:
            listed = ", ".join(sorted(server_params))
            raise ClickHouseQueryError(
                f"statement with a streamed body binds on the client only "
                f"(%(name)s), got server parameters {listed}: {query[:200]!r}"
            )

        if isinstance(text, bytes):
            raise ClickHouseQueryError(
                f"statement with a streamed body must be text: {query[:200]!r}"
            )

        async def insert_body(
            text: str, blocks: AsyncIterable[bytes | bytearray | memoryview]
        ) -> AsyncIterator[bytes | bytearray | memoryview]:
            # возвращает первым сам запрос
            yield text.encode()
            # потом идет разделитель между запросом и данными
            yield b"\n"
            # потом пускаем поток данных
            async for block in blocks:
                yield block

        # аннотация у clickhouse_connect драйвера некорректна
        # он принимает AsyncIterator но не указывает это в аннотациях
        # поэтому приводим к Any типу
        body: Any = insert_body(text, blocks)
        try:
            summary = await client.raw_insert(
                insert_block=body,
                settings=PayloadClickHouse._dict(settings),
                transport_settings=PayloadClickHouse._dict(transport_settings),
            )
        except DriverError as exc:
            raise ClickHouseQueryError(
                f"statement with a streamed body failed: {type(exc).__name__}: "
                f"{exc}; statement: {text[:200]!r}"
            ) from exc

        return PayloadClickHouse._trace_of_summary(summary)

    @staticmethod
    def _trace_of_headers(headers: Mapping[str, str]) -> ChQueryTrace:
        """Сводка из заголовков ответа: у потокового ответа она отправлена до
        конца тела и счётчики чтения в ней — на момент начала ответа."""
        raw = headers.get(ChHeader.SUMMARY.value)
        summary: dict[str, str] = {}
        if raw:
            summary = json.loads(raw)

        return ChQueryTrace(
            summary,
            query_id=PayloadClickHouse._header(headers, ChHeader.QUERY_ID),
            server=PayloadClickHouse._header(headers, ChHeader.SERVER),
            timezone=PayloadClickHouse._header(headers, ChHeader.TIMEZONE),
            fmt=PayloadClickHouse._header(headers, ChHeader.FORMAT),
        )

    @staticmethod
    def _trace_of_summary(summary: QuerySummary) -> ChQueryTrace:
        """Сводка из QuerySummary драйвера после raw_insert: та же JSON-сводка
        сервера, снятая драйвером с заголовков конечного ответа."""
        return ChQueryTrace(
            summary.summary,
            query_id=summary.query_id(),
            server="",
            timezone="",
            fmt="",
        )

    @staticmethod
    def _header(headers: Mapping[str, str], name: ChHeader) -> str:
        value = headers.get(name.value)
        if value is None:
            return ""

        return value

    @staticmethod
    def _params(
        parameters: Mapping[str, Any] | Sequence[Any] | None,
    ) -> dict[str, Any] | list[Any] | None:
        """Параметры в том виде, который принимает драйвер: словарь для
        именованных, список для позиционных."""
        if parameters is None:
            return None

        if isinstance(parameters, Mapping):
            return dict(parameters)

        return list(parameters)

    @staticmethod
    def _dict(values: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if values is None:
            return None

        return dict(values)

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
