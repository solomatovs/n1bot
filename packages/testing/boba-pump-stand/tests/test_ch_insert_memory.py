"""Память сервера ClickHouse на потоковой вставке (pytest -m integration).

Текст вставки и данные потока идут одним телом запроса, и сервер копирует в
память начало тела длиной до настройки max_query_size, чтобы разобрать
стейтмент. В профиле с большим значением (на dev-сервере — гигабайт) это
гигабайт потока в памяти и отказ MEMORY_LIMIT_EXCEEDED на первой же большой
загрузке. Вставка с потоковым телом задаёт max_query_size под свой текст, и
сервер читает данные потоком при любом профиле.

Профиль изображён настройкой клиента: она уходит с каждым запросом, как
настройка профиля пользователя. Память запроса берётся из сводки ответа
сервера; сервер, который её в сводке не отдаёт, пропускается.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pyarrow
import pytest

from boba.db.clickhouse.arrow_stream import ArrowBodyWithoutEos
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.target import ChInsertTuning, InsertMemory
from boba.pump_stand import PumpStand
from boba.toolkit.stream import Chunk
from boba.toolkit.transfer import StreamWire

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()


class BigStream:
    """Поток Arrow заметно больше порога, после которого сервер с большим
    max_query_size держал бы его в памяти сотнями мегабайт."""

    ROWS: ClassVar[int] = 8192
    BATCHES: ClassVar[int] = 700
    """Около 350 МиБ потока."""

    PROFILE_QUERY_SIZE: ClassVar[int] = 1_000_000_000
    LIMIT_BYTES: ClassVar[int] = 256 << 20
    """Потолок памяти запроса: поток читается пачками, а не целиком."""

    INSERT: ClassVar[str] = (
        "insert into function null('id Int32, code Nullable(String)') "
        "select * from input('id Int32, code Nullable(String)') format ArrowStream"
    )

    def __init__(self) -> None:
        self._schema = pyarrow.schema(
            [("id", pyarrow.int32()), ("code", pyarrow.string())]
        )
        codes = [f"{number:048x}" for number in range(self.ROWS)]
        self._batch = pyarrow.record_batch(
            [pyarrow.array(range(self.ROWS), pyarrow.int32()), pyarrow.array(codes)],
            schema=self._schema,
        )

    def rows(self) -> int:
        return self.ROWS * self.BATCHES

    async def frames(self) -> AsyncIterator[Chunk]:
        sink = io.BytesIO()
        writer = pyarrow.ipc.new_stream(sink, self._schema)
        for _ in range(self.BATCHES):
            writer.write_batch(self._batch)
            yield sink.getvalue()
            sink.seek(0)
            sink.truncate()

        writer.close()
        yield sink.getvalue()


@pytest.fixture(params=STAND.demo_clickhouse(), ids=lambda source: source.name)
def profile(request: pytest.FixtureRequest) -> Any:
    return request.param.clickhouse


async def test_stream_is_not_held_in_memory_under_a_large_query_size(
    profile: Any,
) -> None:
    """Профиль с max_query_size в гигабайт: потоковая вставка всё равно
    читает данные потоком, память запроса не растёт с размером потока."""
    stream = BigStream()

    async with PayloadClickHouse.opened_config(profile) as client:
        client.set_client_setting("max_query_size", BigStream.PROFILE_QUERY_SIZE)
        trace = await PayloadClickHouse.byte_stream_in(
            client,
            BigStream.INSERT,
            blocks=ArrowBodyWithoutEos().shaped(stream.frames()),
        )

    if trace.written_rows != stream.rows():
        raise AssertionError(f"every row of the stream is written: {trace}")

    used = trace.memory_usage
    if used is None:
        pytest.skip("the server does not report the memory of a query in the summary")

    if used > BigStream.LIMIT_BYTES:
        raise AssertionError(
            f"the stream is read in batches, got {used >> 20} MiB of query "
            f"memory for a stream of {stream.rows()} rows"
        )


async def test_insert_stays_within_the_memory_it_was_given(profile: Any) -> None:
    """Настройки, посчитанные от разрешённой памяти, держат запрос вставки в
    её пределах: без них сервер копит блоки по своим значениям по умолчанию и
    занимает вдвое больше."""
    stream = BigStream()
    tuning = ChInsertTuning(int(InsertMemory.FLOOR))

    async with PayloadClickHouse.opened_session(profile) as client:
        for statement in tuning.statements(StreamWire.ARROW, 2):
            await PayloadClickHouse.command(client, statement)

        trace = await PayloadClickHouse.byte_stream_in(
            client,
            BigStream.INSERT,
            blocks=ArrowBodyWithoutEos().shaped(stream.frames()),
        )

    if trace.written_rows != stream.rows():
        raise AssertionError(f"every row of the stream is written: {trace}")

    used = trace.memory_usage
    if used is None:
        pytest.skip("the server does not report the memory of a query in the summary")

    if used > InsertMemory.FLOOR:
        raise AssertionError(
            f"the insert was given {InsertMemory.FLOOR >> 20} MiB, "
            f"used {used >> 20} MiB"
        )


async def test_statement_of_the_caller_overrides_the_calculated_one(
    profile: Any,
) -> None:
    """SET вызывающего идёт после посчитанных и перекрывает их: с мелким
    блоком вставки запрос занимает заметно меньше расчётного."""
    stream = BigStream()
    tuning = ChInsertTuning(int(InsertMemory.DEFAULT))

    async with PayloadClickHouse.opened_session(profile) as client:
        for statement in tuning.statements(StreamWire.ARROW, 2):
            await PayloadClickHouse.command(client, statement)

        await PayloadClickHouse.command(client, "SET min_insert_block_size_bytes = 1")
        await PayloadClickHouse.command(client, "SET min_insert_block_size_rows = 8192")
        trace = await PayloadClickHouse.byte_stream_in(
            client,
            BigStream.INSERT,
            blocks=ArrowBodyWithoutEos().shaped(stream.frames()),
        )

    used = trace.memory_usage
    if used is None:
        pytest.skip("the server does not report the memory of a query in the summary")

    if used > InsertMemory.BLOCK_FLOOR * 3:
        raise AssertionError(
            f"the caller's small block is in force, used {used >> 20} MiB"
        )
