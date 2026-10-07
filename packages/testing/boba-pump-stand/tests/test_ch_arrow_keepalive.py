"""Соединение с ClickHouse после потоковой вставки Arrow (pytest -m integration).

Сервер ClickHouse до 25-й версии не дочитывает тело запроса вставки, если
читатель формата остановился раньше его конца: читатель ArrowStream
останавливается на маркере конца потока, завершающий кусок chunked-тела
остаётся в сокете и разбирается началом следующего запроса того же
соединения — тот получает отказ на разборе запроса. Приёмник маркер не
отправляет (ArrowBodyWithoutEos), и сервер дочитывает тело сам.

Гонка в живом клиенте — успел ли завершающий кусок попасть в то же чтение
сокета, что и последние данные. Здесь условие создано наверняка: запрос
идёт сырым сокетом, завершающий кусок уходит позже данных на PAUSE_SEC.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

import base64
import io
import socket
import time
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pyarrow
import pytest

from boba.db.clickhouse.arrow_stream import ArrowBodyWithoutEos
from boba.pump_stand import PumpStand
from boba.toolkit.stream import Chunk

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()


class LateEndInsert:
    """Потоковая вставка Arrow сырым сокетом, у которой завершающий кусок
    chunked-тела уходит позже данных, и следующий запрос того же соединения.

    Создаётся тестом на профиль сервера стенда. Тело вставки — то, что
    приёмник отправил бы серверу: поток Arrow через ArrowBodyWithoutEos.
    """

    ROWS: ClassVar[int] = 200_000
    PAUSE_SEC: ClassVar[float] = 0.3
    READ_SEC: ClassVar[float] = 10.0
    END: ClassVar[bytes] = b"0\r\n\r\n"
    INSERT: ClassVar[str] = (
        "insert into function null('x UInt64') "
        "select x from input('x UInt64') format ArrowStream\n"
    )

    def __init__(self, profile: Any) -> None:
        self._host = str(profile.host)
        self._port = int(profile.port)
        login = f"{profile.auth.user}:{profile.auth.password.get_secret_value()}"
        self._token = base64.b64encode(login.encode()).decode()

    def stream(self) -> bytes:
        """Поток Arrow IPC с одной колонкой x и маркером конца потока."""
        table = pyarrow.table({"x": pyarrow.array(range(self.ROWS), pyarrow.uint64())})
        sink = io.BytesIO()
        with pyarrow.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)

        return sink.getvalue()

    async def body(self) -> bytes:
        """Тело вставки, каким его отправляет приёмник Arrow."""

        async def frames() -> AsyncIterator[Chunk]:
            stream = self.stream()
            yield stream[:-8]
            yield stream[-8:]

        sent = bytearray(self.INSERT.encode())
        async for block in ArrowBodyWithoutEos().shaped(frames()):
            sent.extend(block)

        return bytes(sent)

    def answers(self, body: bytes) -> tuple[str, str]:
        """Первые строки ответов на вставку и на следующий запрос того же
        соединения."""
        with socket.create_connection((self._host, self._port)) as sock:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(self.READ_SEC)
            sock.sendall(self._head("Transfer-Encoding: chunked"))
            sock.sendall(f"{len(body):x}\r\n".encode() + body + b"\r\n")
            time.sleep(self.PAUSE_SEC)
            sock.sendall(self.END)
            inserted = self._answer(sock)

            query = b"select 1"
            sock.sendall(self._head(f"Content-Length: {len(query)}") + query)
            next_one = self._answer(sock)

        return inserted, next_one

    def _head(self, body_header: str) -> bytes:
        lines = [
            "POST / HTTP/1.1",
            f"Host: {self._host}:{self._port}",
            f"Authorization: Basic {self._token}",
            body_header,
            "",
            "",
        ]

        return "\r\n".join(lines).encode()

    def _answer(self, sock: socket.socket) -> str:
        """Статус-строка ответа; ответ дочитан до конца его chunked-тела."""
        data = b""
        while not data.endswith(self.END):
            part = sock.recv(65536)
            if not part:
                break

            data += part

        status, _, _ = data.partition(b"\r\n")

        return status.decode()


@pytest.fixture(params=STAND.demo_clickhouse(), ids=lambda source: source.name)
def profile(request: pytest.FixtureRequest) -> Any:
    return request.param.clickhouse


async def test_connection_serves_the_next_request_after_an_arrow_insert(
    profile: Any,
) -> None:
    """После потоковой вставки Arrow следующий запрос того же соединения
    выполняется: сервер дочитал тело вставки, хвоста в сокете не осталось."""
    insert = LateEndInsert(profile)

    inserted, next_one = insert.answers(await insert.body())

    if inserted != "HTTP/1.1 200 OK":
        raise AssertionError(f"the insert succeeds: {inserted}")
    if next_one != "HTTP/1.1 200 OK":
        raise AssertionError(
            f"the next request of the connection is served, got: {next_one}"
        )
