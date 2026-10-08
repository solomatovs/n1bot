"""Настройки потоковой вставки ClickHouse, посчитанные от разрешённой памяти.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

import pytest

from boba.db.clickhouse.target import ChInsertTuning, InsertMemory
from boba.toolkit.transfer import StreamWire

MIB = 1 << 20
GIB = 1 << 30


class TestBlock:
    """Блок вставки — треть памяти за вычетом постоянной части, в пределах."""

    @pytest.mark.parametrize(
        ("memory", "block"),
        [
            (256 * MIB, (256 - 96) * MIB // 3),
            (512 * MIB, (512 - 96) * MIB // 3),
            (int(InsertMemory.FLOOR), 16 * MIB),
            (1 * GIB, 256 * MIB),
            (8 * GIB, 256 * MIB),
        ],
    )
    def test_block_follows_the_memory(self, memory: int, block: int) -> None:
        if ChInsertTuning(memory).block_bytes() != block:
            raise AssertionError(
                f"memory {memory}: block {ChInsertTuning(memory).block_bytes()}, "
                f"expected {block}"
            )

    def test_three_blocks_and_the_overhead_fit_the_memory(self) -> None:
        """Расчётный пик запроса не выходит за разрешённую память."""
        for memory in (int(InsertMemory.FLOOR), 200 * MIB, 512 * MIB, 3 * GIB):
            peak = InsertMemory.OVERHEAD + 3 * ChInsertTuning(memory).block_bytes()
            if peak > memory:
                raise AssertionError(f"memory {memory}: estimated peak {peak}")


class TestArrow:
    def test_arrow_stream_gets_the_block_and_the_guard(self) -> None:
        memory = 512 * MIB
        block = (512 - 96) * MIB // 3

        chosen = ChInsertTuning(memory).settings(StreamWire.ARROW, 10)

        expected = {
            "max_memory_usage": memory,
            "min_insert_block_size_bytes": block,
            "min_insert_block_size_rows": block,
            "max_insert_threads": 1,
        }
        if chosen != expected:
            raise AssertionError(f"settings of an arrow insert: {chosen}")

    def test_statements_are_sets_in_the_order_of_the_settings(self) -> None:
        tuning = ChInsertTuning(int(InsertMemory.FLOOR))

        steps = tuning.statements(StreamWire.ARROW, 3)

        expected = (
            f"SET max_memory_usage = {int(InsertMemory.FLOOR)}",
            f"SET min_insert_block_size_bytes = {16 * MIB}",
            f"SET min_insert_block_size_rows = {16 * MIB}",
            "SET max_insert_threads = 1",
        )
        if steps != expected:
            raise AssertionError(f"statements of the session start: {steps}")


class TestText:
    """Данные текстом: блок разбора в строках и параллельный разбор."""

    def test_parse_block_follows_the_insert_block_and_the_row_width(self) -> None:
        tuning = ChInsertTuning(512 * MIB)
        block = tuning.block_bytes()

        chosen = tuning.settings(StreamWire.TSV, 10)

        if chosen["max_insert_block_size"] != block // (10 * 16):
            raise AssertionError(f"rows of the parse block: {chosen}")
        if chosen["input_format_parallel_parsing"] != 0:
            raise AssertionError(f"no memory for parallel parsing: {chosen}")

    def test_parse_block_stays_within_its_limits(self) -> None:
        narrow = ChInsertTuning(8 * GIB).settings(StreamWire.TSV, 1)
        wide = ChInsertTuning(int(InsertMemory.FLOOR)).settings(StreamWire.CSV, 2000)

        if narrow["max_insert_block_size"] != ChInsertTuning.PARSE_ROWS_CEIL:
            raise AssertionError(f"a narrow row hits the ceiling: {narrow}")
        if wide["max_insert_block_size"] != ChInsertTuning.PARSE_ROWS_FLOOR:
            raise AssertionError(f"a wide row hits the floor: {wide}")

    def test_parallel_parsing_is_on_when_the_memory_covers_it(self) -> None:
        enough = InsertMemory.OVERHEAD + 3 * InsertMemory.BLOCK_CEIL
        enough += InsertMemory.PARALLEL_PARSING

        short = ChInsertTuning(enough - 1).settings(StreamWire.TSV, 10)
        covered = ChInsertTuning(enough).settings(StreamWire.TSV, 10)

        if short["input_format_parallel_parsing"] != 0:
            raise AssertionError(f"one byte short keeps it off: {short}")
        if covered["input_format_parallel_parsing"] != 1:
            raise AssertionError(f"enough memory turns it on: {covered}")
