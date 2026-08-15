"""Окно живого вывода: вытеснение, закрытие, пробуждения."""

from __future__ import annotations

import pytest

from boba.toolkit.stream import ToolStreamBuffer


class TestWindow:
    """Буфер держит хвост фиксированного объёма и считает вытесненное."""

    @staticmethod
    def _buffer(window: int, wakes: list[int]) -> ToolStreamBuffer:
        def wake() -> None:
            wakes.append(1)

        return ToolStreamBuffer(window, wake)

    def test_tail_survives_eviction(self) -> None:
        wakes: list[int] = []
        buffer = self._buffer(10, wakes)

        buffer.feed(b"12345")
        buffer.feed(b"67890")
        buffer.feed(b"abc")

        window = buffer.snapshot()
        assert window.text == "4567890abc"
        assert window.dropped_bytes == 3

    def test_chunk_larger_than_window_keeps_its_tail(self) -> None:
        buffer = self._buffer(10, [])

        buffer.feed(b"x" * 90 + b"0123456789")

        window = buffer.snapshot()
        assert window.text == "0123456789"
        assert window.dropped_bytes == 90

    def test_close_freezes_window(self) -> None:
        buffer = self._buffer(100, [])

        buffer.feed(b"until close")
        buffer.close("rc=0")
        buffer.feed(b"after close")
        buffer.close("другая причина")

        window = buffer.snapshot()
        assert window.text == "until close"
        assert window.closed is True
        assert window.note == "rc=0"

    def test_wake_on_data_and_close_only(self) -> None:
        wakes: list[int] = []
        buffer = self._buffer(100, wakes)

        buffer.feed(b"")
        assert wakes == []

        buffer.feed(b"data")
        buffer.close("done")
        assert len(wakes) == 2

    def test_multibyte_cut_does_not_break_snapshot(self) -> None:
        buffer = self._buffer(3, [])

        buffer.feed("яя".encode())

        window = buffer.snapshot()
        assert window.dropped_bytes == 1
        assert window.text.endswith("я")

    def test_window_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="window_bytes must be positive"):
            self._buffer(0, [])
