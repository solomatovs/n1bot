"""Байтовые коллекторы порта: сборка текста и NDJSON-строк из потока канала."""

from __future__ import annotations

import pytest

from boba.toolkit.channels import StreamCodec
from boba.toolkit.launcher import (
    CollectorCapacityError,
    CollectorRowLimitError,
    LauncherError,
    RowCollector,
    TextCollector,
)


class TestTextCollector:
    def test_collects_text_across_feeds(self) -> None:
        collector = TextCollector(max_chars=100, limit_rows=None, header_lines=0)
        collector.feed(b"head")
        collector.feed(b"er\nrow\n")
        collector.close()
        assert collector.text() == "header\nrow\n"
        assert collector.row_count == 2

    def test_multibyte_split_between_feeds_survives(self) -> None:
        """Инкрементальный декодер: utf-8 символ, разрезанный границей чтения."""
        data = "привет".encode()
        collector = TextCollector(max_chars=100, limit_rows=None, header_lines=0)
        collector.feed(data[:3])
        collector.feed(data[3:])
        collector.close()
        assert collector.text() == "привет"

    def test_header_lines_are_not_rows(self) -> None:
        collector = TextCollector(max_chars=100, limit_rows=None, header_lines=1)
        collector.feed(b"h\na\nb\n")
        assert collector.row_count == 2

    def test_capacity_limit_raises(self) -> None:
        collector = TextCollector(max_chars=4, limit_rows=None, header_lines=0)
        with pytest.raises(CollectorCapacityError):
            collector.feed(b"12345")

    def test_row_limit_raises(self) -> None:
        collector = TextCollector(max_chars=100, limit_rows=1, header_lines=0)
        with pytest.raises(CollectorRowLimitError):
            collector.feed(b"a\nb\n")


class TestRowCollector:
    def test_rows_across_feed_boundaries(self) -> None:
        collector = RowCollector(max_chars=1000, limit_rows=None)
        collector.feed(b'{"a": 1}\n{"a"')
        collector.feed(b': 2}\n')
        collector.close()
        assert collector.rows() == [{"a": 1}, {"a": 2}]
        assert collector.row_count == 2

    def test_last_line_without_newline_counts(self) -> None:
        collector = RowCollector(max_chars=1000, limit_rows=None)
        collector.feed(b'{"a": 1}')
        collector.close()
        assert collector.rows() == [{"a": 1}]

    def test_blank_lines_are_skipped(self) -> None:
        collector = RowCollector(max_chars=1000, limit_rows=None)
        collector.feed(b'\n{"a": 1}\n\n')
        collector.close()
        assert collector.rows() == [{"a": 1}]

    def test_row_limit_raises(self) -> None:
        collector = RowCollector(max_chars=1000, limit_rows=1)
        with pytest.raises(CollectorRowLimitError):
            collector.feed(b'{"a": 1}\n{"a": 2}\n')

    def test_capacity_limit_raises(self) -> None:
        collector = RowCollector(max_chars=5, limit_rows=None)
        with pytest.raises(CollectorCapacityError):
            collector.feed(b'{"a": 1}\n')

    def test_broken_line_is_launcher_error(self) -> None:
        collector = RowCollector(max_chars=1000, limit_rows=None)
        with pytest.raises(LauncherError):
            collector.feed(b"not-json\n")


class TestStreamCodec:
    def test_encode_line_round_trip(self) -> None:
        line = StreamCodec.encode_row({"имя": "значение"})
        assert line.endswith(b"\n")
        collector = RowCollector(max_chars=1000, limit_rows=None)
        collector.feed(line)
        assert collector.rows() == [{"имя": "значение"}]
