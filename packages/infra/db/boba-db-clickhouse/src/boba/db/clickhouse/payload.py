"""ClickHouse для payload'ов; пула нет — каждый вызов свой процесс и клиент.
Учётные данные приходят через stdin: не видны ни в argv, ни в /proc, ни в логах.

Ошибки:
ClickHouseQueryError — сервер отклонил запрос, оборвал ответ до шапки
    с именами колонок или прислал шапку, которую не разобрать, или чтению
    заданы размеры, которые не применить.
ClickHouseError — до базы не достучаться (сеть, TLS, kerberos, отказ
клиента при инициализации)."""

from __future__ import annotations

import json
from abc import abstractmethod
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
from enum import StrEnum
from typing import Any, ClassVar, Protocol, cast, runtime_checkable

import aiohttp
from clickhouse_connect.datatypes.base import ClickHouseType
from clickhouse_connect.datatypes.registry import get_from_name
from clickhouse_connect.driver._backend.http_async import release_lease
from clickhouse_connect.driver.asyncclient import AsyncClient
from clickhouse_connect.driver.binding import bind_query
from clickhouse_connect.driver.exceptions import ClickHouseError as DriverError
from clickhouse_connect.driver.external import ExternalData
from clickhouse_connect.driver.query import QueryContext, QueryResult, TzMode
from clickhouse_connect.driver.summary import QuerySummary
from pydantic import TypeAdapter

from boba.db.clickhouse.connection import ClickHouseConfig
from boba.db.clickhouse.errors import ClickHouseError, ClickHouseQueryError
from boba.kerberos import KerberosAuthBase, KerberosError
from boba.krb import ClientCredentials, SpnegoNegotiate

__all__ = [
    "ByteStream",
    "JsonCompactStream",
    "JsonExactOutput",
    "PayloadClickHouse",
    "ReadTuning",
    "RowStream",
    "SpnegoHeaders",
    "StreamFormat",
    "TsvStream",
]


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
    выбрал вызывающий: блоки идут как пришли, без разбора. На нём строятся
    потоки конкретных форматов, например TsvStream."""

    blocks: AsyncIterator[memoryview]


@dataclass(frozen=True)
class TsvStream:
    """Ответ запроса в формате TabSeparated: имена и типы колонок из шапки
    TabSeparatedWithNamesAndTypes и сырые байты строк после неё. type_names —
    типы так, как их написал сервер, они же уходят обратно в tsv_stream_in;
    column_types — те же объекты драйвера, что у RowStream (имя типа у
    драйвера бывает другим: именованный Tuple он пишет с обратными
    кавычками)."""

    names: tuple[str, ...]
    type_names: tuple[str, ...]
    column_types: tuple[ClickHouseType, ...]
    blocks: AsyncIterator[memoryview]


@dataclass(frozen=True)
class JsonCompactStream:
    """Ответ запроса в формате JSONCompactEachRowWithNamesAndTypes: имена и
    типы колонок из первых двух строк-массивов и сырые байты строк после них,
    по JSON-массиву на строку. type_names и column_types — как у TsvStream."""

    names: tuple[str, ...]
    type_names: tuple[str, ...]
    column_types: tuple[ClickHouseType, ...]
    blocks: AsyncIterator[memoryview]


@dataclass(frozen=True)
class HeadedColumns:
    """Имена и типы колонок, снятые с шапки потока, и блоки после шапки; общая
    часть TsvStream и JsonCompactStream."""

    names: tuple[str, ...]
    type_names: tuple[str, ...]
    column_types: tuple[ClickHouseType, ...]
    blocks: AsyncIterator[memoryview]


@dataclass(frozen=True)
class HeadLines:
    """Строки шапки без переводов строки и байты, пришедшие следом за ней."""

    lines: tuple[bytes, ...]
    rest: bytes


class StreamFormat(StrEnum):
    """Форматы, которые потоки payload ставят запросу сами."""

    RAW_BLOB = "RawBLOB"
    TSV_WITH_NAMES_AND_TYPES = "TabSeparatedWithNamesAndTypes"
    JSON_COMPACT_WITH_NAMES_AND_TYPES = "JSONCompactEachRowWithNamesAndTypes"
    JSON_EACH_ROW = "JSONEachRow"
    JSON_AS_STRING = "JSONAsString"

    def insert(self, query: str) -> str:
        """Текст вставки `INSERT INTO ...` с форматом тела, как дописывает
        FORMAT сам драйвер."""
        return f"{query}\n FORMAT {self.value}"


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


class TsvEscape(StrEnum):
    """Буквы escape-последовательностей TabSeparated, означающие управляющий
    символ. Кроме них ClickHouse экранирует только `\\\\` и `\\'`, где символ после
    слэша означает сам себя; всё остальное, включая прочие управляющие символы
    и не-ASCII, пишется как есть."""

    TAB = "t"
    NEWLINE = "n"
    RETURN = "r"
    BACKSPACE = "b"
    FORM_FEED = "f"
    NUL = "0"

    def char(self) -> str:
        match self:
            case TsvEscape.TAB:
                return "\t"
            case TsvEscape.NEWLINE:
                return "\n"
            case TsvEscape.RETURN:
                return "\r"
            case TsvEscape.BACKSPACE:
                return "\b"
            case TsvEscape.FORM_FEED:
                return "\f"
            case TsvEscape.NUL:
                return "\0"


class HeaderLine(Protocol):
    """Строка шапки потока с именами или типами колонок: разбор пришедшей
    строки без перевода строки и запись строки с переводом строки в конце.
    Реализации — TsvHeader и JsonCompactHeader, зовёт их StreamHead."""

    @abstractmethod
    def parse(self, line: bytes) -> tuple[str, ...]: ...

    @abstractmethod
    def render(self, values: Sequence[str]) -> bytes: ...


class TsvHeader(HeaderLine):
    """Реализация HeaderLine для TabSeparatedWithNamesAndTypes: разбор и запись
    строки шапки (имён или типов). Сырая табуляция в шапке бывает только
    разделителем, потому что табуляцию внутри имени сервер пишет как `\\t`;
    поэтому строка сначала делится по табуляции, а потом в каждом имени
    раскрываются escape-последовательности. Запись экранирует ровно те символы, которые
    экранирует сам сервер: `\\\\`, `\\'` и буквы TsvEscape."""

    SEPARATOR: ClassVar[str] = "\t"
    ESCAPE: ClassVar[str] = "\\"
    ENCODING: ClassVar[str] = "utf-8"
    LINE_END: ClassVar[bytes] = b"\n"
    QUOTE: ClassVar[str] = "'"

    def __init__(self) -> None:
        self._escapes: dict[str, str] = {}
        for escape in TsvEscape:
            self._escapes[escape.char()] = self.ESCAPE + escape.value

        self._escapes[self.ESCAPE] = self.ESCAPE + self.ESCAPE
        self._escapes[self.QUOTE] = self.ESCAPE + self.QUOTE

    def render(self, values: Sequence[str]) -> bytes:
        """Строка шапки с переводом строки в конце."""
        fields: list[str] = []
        for value in values:
            fields.append(self._escaped(value))

        line = self.SEPARATOR.join(fields)

        return line.encode(self.ENCODING) + self.LINE_END

    def parse(self, line: bytes) -> tuple[str, ...]:
        text = line.decode(self.ENCODING)

        names: list[str] = []
        for field in text.split(self.SEPARATOR):
            names.append(self._unescaped(field))

        return tuple(names)

    def _unescaped(self, field: str) -> str:
        if self.ESCAPE not in field:
            return field

        chars: list[str] = []
        escaped = False
        for char in field:
            if escaped:
                chars.append(self._escape_of(char))
                escaped = False
                continue

            if char == self.ESCAPE:
                escaped = True
                continue

            chars.append(char)

        return "".join(chars)

    def _escape_of(self, char: str) -> str:
        try:
            escape = TsvEscape(char)
        except ValueError:
            return char

        return escape.char()

    def _escaped(self, value: str) -> str:
        chars: list[str] = []
        for char in value:
            chars.append(self._escapes.get(char, char))

        return "".join(chars)


class JsonExactOutput(StrEnum):
    """Настройки вывода JSON, при которых путь через JSON и обратно в
    ClickHouse совпадает с TSV байт в байт, а 22.x и новые версии пишут
    одинаковые байты. Без них NaN и Inf уходят null и возвращаются нулём,
    а 64-битные целые одни версии пишут строкой, другие числом."""

    QUOTE_DENORMALS = "output_format_json_quote_denormals"
    QUOTE_64BIT_INTEGERS = "output_format_json_quote_64bit_integers"
    QUOTE_64BIT_FLOATS = "output_format_json_quote_64bit_floats"
    QUOTE_DECIMALS = "output_format_json_quote_decimals"
    VALIDATE_UTF8 = "output_format_json_validate_utf8"
    ESCAPE_FORWARD_SLASHES = "output_format_json_escape_forward_slashes"

    def value_of(self) -> int:
        """Значение настройки: всё включено, кроме замены невалидного UTF-8,
        которая портит бинарные строки и FixedString."""
        match self:
            case JsonExactOutput.VALIDATE_UTF8:
                return 0
            case _:
                return 1


class JsonCompactHeader(HeaderLine):
    """Реализация HeaderLine для JSONCompactEachRowWithNamesAndTypes: строка
    шапки — JSON-массив строк. Разбирает и проверяет его pydantic, пишет
    json.dumps; экранирование целиком по правилам JSON."""

    LINE_END: ClassVar[bytes] = b"\n"
    ENCODING: ClassVar[str] = "utf-8"

    def __init__(self) -> None:
        self._values = TypeAdapter(tuple[str, ...])

    def parse(self, line: bytes) -> tuple[str, ...]:
        return self._values.validate_json(line)

    def render(self, values: Sequence[str]) -> bytes:
        listed = list(values)
        text = json.dumps(listed, ensure_ascii=False)

        return text.encode(self.ENCODING) + self.LINE_END


class StreamHead:
    """Шапка текстового потока с именами и типами колонок в первых двух
    строках: снимает их с блоков ответа, строит типы драйвером и отдаёт блоки
    после шапки как есть; на входе пишет такую шапку перед блоками. Разбор
    строк шапки — у HeaderLine формата; зовут PayloadClickHouse._headed_out и
    _headed_in."""

    LINE_END: ClassVar[bytes] = b"\n"
    LINES: ClassVar[int] = 2

    def __init__(self, fmt: StreamFormat, header: HeaderLine, query: str) -> None:
        self._fmt = fmt
        self._header = header
        self._query = query

    async def columns(self, chunks: AsyncIterator[memoryview]) -> HeadedColumns:
        head = await self._lines(chunks)
        names = self._parsed(head.lines[0], "column names")
        type_names = self._parsed(head.lines[1], "column types")
        column_types = self._types(type_names)

        return HeadedColumns(
            names=names,
            type_names=type_names,
            column_types=column_types,
            blocks=self.glued(head.rest, chunks),
        )

    def rendered(self, names: Sequence[str], type_names: Sequence[str]) -> bytes:
        return self._header.render(names) + self._header.render(type_names)

    async def glued(
        self, head: bytes, source: AsyncIterator[memoryview]
    ) -> AsyncIterator[memoryview]:
        if head:
            yield memoryview(head)

        async for block in source:
            yield block

    async def _lines(self, chunks: AsyncIterator[memoryview]) -> HeadLines:
        head = bytearray()
        lines: list[bytes] = []
        start = 0
        while len(lines) < self.LINES:
            end = head.find(self.LINE_END, start)
            if end >= 0:
                lines.append(bytes(head[start:end]))
                start = end + 1
                continue

            chunk = await anext(chunks, None)
            if chunk is None:
                raise ClickHouseQueryError(
                    f"reading {self._fmt} header: expected lines of column names "
                    f"and types, the response ended after {len(head)} bytes; "
                    f"query: {self._query[:200]!r}"
                )

            head.extend(chunk)

        return HeadLines(lines=tuple(lines), rest=bytes(head[start:]))

    def _parsed(self, line: bytes, what: str) -> tuple[str, ...]:
        try:
            return self._header.parse(line)
        except ValueError as exc:
            raise ClickHouseQueryError(
                f"reading {self._fmt} header: expected a line of {what}, got "
                f"{line[:200]!r}: {exc}; query: {self._query[:200]!r}"
            ) from exc

    def _types(self, type_names: Sequence[str]) -> tuple[ClickHouseType, ...]:
        column_types: list[ClickHouseType] = []
        for type_name in type_names:
            try:
                column_types.append(get_from_name(type_name))
            except DriverError as exc:
                raise ClickHouseQueryError(
                    f"reading {self._fmt} header: expected a clickhouse type name, "
                    f"got {type_name!r}: {exc}; query: {self._query[:200]!r}"
                ) from exc

        return tuple(column_types)


class PayloadClickHouse:
    """Исполнитель запросов ClickHouse: принимает готовый текст, параметры и
    настройки драйвера как есть и ничего не знает о том, как их собрали."""

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
        fmt: str,
        parameters: Mapping[str, Any] | Sequence[Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        use_database: bool = True,
        external_data: ExternalData | None = None,
        transport_settings: Mapping[str, str] | None = None,
        tuning: ReadTuning | None = None,
    ) -> AsyncGenerator[ByteStream, None]:
        """
        Выполняет запрос и отдаёт сырой http-поток ответа блоками в формате fmt,
        без какого-либо разбора и преобразования в python объекты.
        Если тебе хочется передать FORMAT в тексте запроса то делай это
        через передачу в аргументе fmt, потому что драйвер заточен
        подставлять этот аргумент в текст запроса

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

            yield ByteStream(blocks=blocks(response.content))
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
    @asynccontextmanager
    async def blob_stream_out(  # noqa: PLR0913
        client: AsyncClient,
        query: str,
        parameters: Mapping[str, Any] | Sequence[Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        use_database: bool = True,
        external_data: ExternalData | None = None,
        transport_settings: Mapping[str, str] | None = None,
        tuning: ReadTuning | None = None,
    ) -> AsyncGenerator[ByteStream, None]:
        """
        Поток byte_stream_out в формате RawBLOB. Аргументы — те же, что у
        byte_stream_out, кроме fmt; tuning задаёт размер блоков (ReadTuning).

        Работает так:
            - подставляет в запрос format=RawBLOB, и сервер отдаёт значения
                всех строк и всех колонок подряд, без разделителей и без
                экранирования; строки — как есть, числа — двоичными little-endian,
                NULL и пустой результат — ноль байт
            - границ значений в потоке нет, поэтому байты отдаются как есть;
                формат для одной колонки и одного значения: файла, картинки,
                документа
        """
        async with PayloadClickHouse.byte_stream_out(
            client,
            query,
            StreamFormat.RAW_BLOB,
            parameters,
            settings,
            use_database,
            external_data,
            transport_settings,
            tuning,
        ) as stream:
            yield stream

    @staticmethod
    @asynccontextmanager
    async def tsv_stream_out(  # noqa: PLR0913
        client: AsyncClient,
        query: str,
        parameters: Mapping[str, Any] | Sequence[Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        use_database: bool = True,
        external_data: ExternalData | None = None,
        transport_settings: Mapping[str, str] | None = None,
        tuning: ReadTuning | None = None,
    ) -> AsyncGenerator[TsvStream, None]:
        """
        Поток byte_stream_out в формате TabSeparatedWithNamesAndTypes
        Аргументы — те же, что у byte_stream_out, но fmt проставляется автоматически

        Работает так:
            - подставляет в запрос format=TabSeparatedWithNamesAndTypes
                из-за чего clickhouse начинает отдавать поток в формате tsv,
                где первая строка — имена колонок, вторая — их типы
            - имена и типы снимаются с этих двух строк, в том числе у пустого
                результата, типы строит драйвер по их именам
            - дальше байты идут как есть
        Экранирование TabSeparated совпадает с текстовым форматом COPY
        PostgreSQL, где значение NULL это \\N.
        Позволяет получить PostgreSQL совместимый поток, который можно отправлять
        напрямую в copy_from
        """
        async with PayloadClickHouse._headed_out(
            client,
            query,
            StreamFormat.TSV_WITH_NAMES_AND_TYPES,
            TsvHeader(),
            parameters,
            settings,
            use_database,
            external_data,
            transport_settings,
            tuning,
        ) as columns:
            yield TsvStream(
                names=columns.names,
                type_names=columns.type_names,
                column_types=columns.column_types,
                blocks=columns.blocks,
            )

    @staticmethod
    @asynccontextmanager
    async def json_compact_stream_out(  # noqa: PLR0913
        client: AsyncClient,
        query: str,
        parameters: Mapping[str, Any] | Sequence[Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        use_database: bool = True,
        external_data: ExternalData | None = None,
        transport_settings: Mapping[str, str] | None = None,
        tuning: ReadTuning | None = None,
    ) -> AsyncGenerator[JsonCompactStream, None]:
        """
        Поток byte_stream_out в формате JSONCompactEachRowWithNamesAndTypes.
        Аргументы — те же, что у byte_stream_out, но fmt проставляется
        автоматически.

        Работает так:
            - первая строка ответа — JSON-массив имён колонок, вторая — их
                типов; их разбирает JSON, типы строит драйвер по именам
            - дальше по JSON-массиву на строку, байты идут как есть
            - запросу ставятся настройки JsonExactOutput: NaN, Inf, 64-битные
                числа и Decimal уходят строками, невалидный UTF-8 не
                заменяется; путь через json_compact_stream_in обратно в
                ClickHouse совпадает с TSV байт в байт, а 22.x и новые версии
                пишут одинаковые байты. Свой settings перекрывает их, и тогда
                определённость на совести вызывающего.
        Строки с невалидным UTF-8 идут сырыми байтами: ClickHouse примет их
        обратно, строгий JSON-парсер — нет.
        """
        chosen: dict[str, Any] = {}
        for setting in JsonExactOutput:
            chosen[setting.value] = setting.value_of()

        if settings:
            chosen.update(settings)

        async with PayloadClickHouse._headed_out(
            client,
            query,
            StreamFormat.JSON_COMPACT_WITH_NAMES_AND_TYPES,
            JsonCompactHeader(),
            parameters,
            chosen,
            use_database,
            external_data,
            transport_settings,
            tuning,
        ) as columns:
            yield JsonCompactStream(
                names=columns.names,
                type_names=columns.type_names,
                column_types=columns.column_types,
                blocks=columns.blocks,
            )

    @staticmethod
    async def byte_stream_in(  # noqa: PLR0913
        client: AsyncClient,
        query: str,
        parameters: Mapping[str, Any] | Sequence[Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        transport_settings: Mapping[str, str] | None = None,
        *,
        blocks: AsyncIterable[bytes | bytearray | memoryview],
    ) -> QuerySummary:
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

        return summary

    @staticmethod
    async def tsv_stream_in(  # noqa: PLR0913
        client: AsyncClient,
        query: str,
        parameters: Mapping[str, Any] | Sequence[Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        transport_settings: Mapping[str, str] | None = None,
        *,
        stream: TsvStream,
    ) -> QuerySummary:
        """
        Зеркало tsv_stream_out: вставка потока TsvStream через byte_stream_in в
        формате TabSeparatedWithNamesAndTypes. Аргументы — те же, что у
        byte_stream_in, вместо blocks — stream.

        Работает так:
            - к запросу `INSERT INTO ...` дописывается FORMAT
                TabSeparatedWithNamesAndTypes, свой FORMAT в тексте не указывается
            - первыми строками тела уходят имена и типы колонок из stream,
                дальше блоки stream как есть
            - сервер сопоставляет колонки по именам из шапки, тип в шапке
                обязан совпасть с типом колонки, иначе вставка отклоняется
            - колонку шапки, которой нет в таблице, сервер по умолчанию молча
                пропускает (input_format_skip_unknown_fields = 1); чтобы это
                было ошибкой, настройка передаётся в settings
        """
        return await PayloadClickHouse._headed_in(
            client,
            query,
            StreamFormat.TSV_WITH_NAMES_AND_TYPES,
            TsvHeader(),
            parameters,
            settings,
            transport_settings,
            names=stream.names,
            type_names=stream.type_names,
            blocks=stream.blocks,
        )

    @staticmethod
    async def json_compact_stream_in(  # noqa: PLR0913
        client: AsyncClient,
        query: str,
        parameters: Mapping[str, Any] | Sequence[Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        transport_settings: Mapping[str, str] | None = None,
        *,
        stream: JsonCompactStream,
    ) -> QuerySummary:
        """
        Зеркало json_compact_stream_out: вставка потока JsonCompactStream через
        byte_stream_in в формате JSONCompactEachRowWithNamesAndTypes.
        Аргументы — те же, что у byte_stream_in, вместо blocks — stream.

        Работает так же, как tsv_stream_in: к `INSERT INTO ...` дописывается
        FORMAT, первыми строками тела уходят JSON-массивы имён и типов, сервер
        сопоставляет колонки по именам, отклоняет несовпавший тип и по
        умолчанию молча пропускает лишнюю колонку
        (input_format_skip_unknown_fields).
        """
        return await PayloadClickHouse._headed_in(
            client,
            query,
            StreamFormat.JSON_COMPACT_WITH_NAMES_AND_TYPES,
            JsonCompactHeader(),
            parameters,
            settings,
            transport_settings,
            names=stream.names,
            type_names=stream.type_names,
            blocks=stream.blocks,
        )

    @staticmethod
    async def jsonl_stream_in(  # noqa: PLR0913
        client: AsyncClient,
        query: str,
        parameters: Mapping[str, Any] | Sequence[Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        transport_settings: Mapping[str, str] | None = None,
        *,
        blocks: AsyncIterable[bytes | bytearray | memoryview],
    ) -> QuerySummary:
        """
        Вставка JSON-документов с записями через byte_stream_in в формате
        JSONEachRow. Аргументы — те же, что у byte_stream_in.

        Принимает как есть и потоком:
            - JSON Lines: объект на строку
            - один JSON-массив объектов, в строку или отформатированный
            - одиночный объект — одна строка
        Колонки сопоставляются по ключам объекта; ключ, которого нет в таблице,
        по умолчанию молча пропускается, а отсутствующий ключ даёт значение
        колонки по умолчанию. Из-за этого документ, где записи лежат не в
        корне ({"items": [...]}), молча станет одной строкой из умолчаний;
        input_format_skip_unknown_fields = 0 в settings делает это ошибкой.
        Такие документы вставляет json_document_stream_in.
        """
        return await PayloadClickHouse.byte_stream_in(
            client,
            StreamFormat.JSON_EACH_ROW.insert(query),
            parameters,
            settings,
            transport_settings,
            blocks=blocks,
        )

    @staticmethod
    async def json_document_stream_in(  # noqa: PLR0913
        client: AsyncClient,
        query: str,
        parameters: Mapping[str, Any] | Sequence[Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        transport_settings: Mapping[str, str] | None = None,
        *,
        blocks: AsyncIterable[bytes | bytearray | memoryview],
    ) -> QuerySummary:
        """
        Вставка произвольных JSON-документов через byte_stream_in в формате
        JSONAsString. Аргументы — те же, что у byte_stream_in.

        Каждый JSON-документ верхнего уровня из тела — одна строка, документ
        целиком ложится в единственную колонку String таблицы (или в колонку,
        названную в `INSERT INTO t (doc)`); разбирают его потом в SQL
        функциями JSONExtract*. Документы в теле идут подряд, через перевод
        строки или без него.
        """
        return await PayloadClickHouse.byte_stream_in(
            client,
            StreamFormat.JSON_AS_STRING.insert(query),
            parameters,
            settings,
            transport_settings,
            blocks=blocks,
        )

    @staticmethod
    @asynccontextmanager
    async def _headed_out(  # noqa: PLR0913
        client: AsyncClient,
        query: str,
        fmt: StreamFormat,
        header: HeaderLine,
        parameters: Mapping[str, Any] | Sequence[Any] | None,
        settings: Mapping[str, Any] | None,
        use_database: bool,
        external_data: ExternalData | None,
        transport_settings: Mapping[str, str] | None,
        tuning: ReadTuning | None,
    ) -> AsyncGenerator[HeadedColumns, None]:
        """Поток byte_stream_out в формате с шапкой имён и типов: шапку снимает
        StreamHead, дальше блоки как есть."""
        async with PayloadClickHouse.byte_stream_out(
            client,
            query,
            fmt,
            parameters,
            settings,
            use_database,
            external_data,
            transport_settings,
            tuning,
        ) as raw:
            head = StreamHead(fmt, header, query)
            yield await head.columns(raw.blocks)

    @staticmethod
    async def _headed_in(  # noqa: PLR0913
        client: AsyncClient,
        query: str,
        fmt: StreamFormat,
        header: HeaderLine,
        parameters: Mapping[str, Any] | Sequence[Any] | None,
        settings: Mapping[str, Any] | None,
        transport_settings: Mapping[str, str] | None,
        *,
        names: Sequence[str],
        type_names: Sequence[str],
        blocks: AsyncIterator[memoryview],
    ) -> QuerySummary:
        """Вставка через byte_stream_in в формате с шапкой: к запросу
        дописывается FORMAT, шапку имён и типов пишет StreamHead перед блоками."""
        head = StreamHead(fmt, header, query)
        rendered = head.rendered(names, type_names)

        return await PayloadClickHouse.byte_stream_in(
            client,
            fmt.insert(query),
            parameters,
            settings,
            transport_settings,
            blocks=head.glued(rendered, blocks),
        )

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
