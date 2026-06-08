"""Unit: CopyBuffer (без БД)."""

from __future__ import annotations

import pytest

from boba.tool.pg.copy_buffer import (
    BufferCapacityError,
    CopyBuffer,
    RowLimitExceededError,
)


def test_accumulates_blocks() -> None:
    buf = CopyBuffer(max_capacity=1024)
    buf.write(b"id,name\n")
    buf.write(b"1,prod\n")
    assert buf.size == len(b"id,name\n1,prod\n")
    assert buf.decode() == "id,name\n1,prod\n"


def test_accepts_memoryview_blocks() -> None:
    """psycopg отдаёт Buffer (memoryview) — slice-assign должен его принять."""
    buf = CopyBuffer(max_capacity=1024)
    buf.write(memoryview(b"ab"))
    buf.write(memoryview(b"cd"))
    assert buf.decode() == "abcd"


def test_grows_past_initial_capacity() -> None:
    """Запись больше _INITIAL_CAPACITY (4096) триггерит рост."""
    buf = CopyBuffer(max_capacity=1_000_000)
    big = b"x" * 10_000
    buf.write(big)
    assert buf.size == 10_000
    assert buf.decode() == big.decode()


def test_multibyte_split_across_writes_decodes_clean() -> None:
    """UTF-8 символ, разрезанный между write'ами, декодится без порчи.

    Декод один раз в конце — split на границе блока не ломает символ.
    """
    text = "Ы2байта 🦊4байта ©"  # кириллица 2б, эмодзи 4б, © 2б
    payload = text.encode()
    buf = CopyBuffer(max_capacity=1024)
    for i in range(len(payload)):  # по одному байту — максимум разрезов символов
        buf.write(payload[i : i + 1])
    assert buf.decode() == text


def test_exact_capacity_fits() -> None:
    buf = CopyBuffer(max_capacity=4)
    buf.write(b"abcd")
    assert buf.decode() == "abcd"


def test_over_capacity_raises() -> None:
    buf = CopyBuffer(max_capacity=4)
    with pytest.raises(BufferCapacityError):
        buf.write(b"abcde")


def test_over_capacity_across_two_writes() -> None:
    buf = CopyBuffer(max_capacity=4)
    buf.write(b"abc")
    with pytest.raises(BufferCapacityError):
        buf.write(b"de")


def test_rejects_nonpositive_capacity() -> None:
    with pytest.raises(ValueError, match="max_capacity"):
        CopyBuffer(max_capacity=0)


# --------------------------------------------------------------------------- #
# трекинг строк / limit_rows
# --------------------------------------------------------------------------- #


def test_row_count_excludes_header() -> None:
    """row_count = строк минус header (по числу \\n)."""
    buf = CopyBuffer(max_capacity=1024)
    buf.write(b"h1\th2\n")  # header
    buf.write(b"1\t2\n")
    buf.write(b"3\t4\n")
    assert buf.row_count == 2


def test_row_count_counts_separators_across_blocks() -> None:
    """\\n на границе блока считается верно — count по факту записи."""
    buf = CopyBuffer(max_capacity=1024)
    buf.write(b"ab")
    buf.write(b"c\nde")  # первый \n приходит во втором блоке (header "abc")
    buf.write(b"f\n")
    assert buf.row_count == 1  # 2 строки (abc, def) минус header


def test_limit_rows_raises_when_exceeded() -> None:
    buf = CopyBuffer(max_capacity=1024, limit_rows=2)
    buf.write(b"h\n")  # header -> row_count 0
    buf.write(b"a\n")  # 1
    buf.write(b"b\n")  # 2
    with pytest.raises(RowLimitExceededError):
        buf.write(b"c\n")  # 3 > 2


def test_limit_rows_boundary_ok() -> None:
    """Ровно limit_rows data-строк — без ошибки."""
    buf = CopyBuffer(max_capacity=1024, limit_rows=3)
    buf.write(b"h\na\nb\nc\n")
    assert buf.row_count == 3
