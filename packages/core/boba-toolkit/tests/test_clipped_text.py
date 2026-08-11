"""ClippedText: усечение потока по байтовому потолку с признаком усечения."""

from __future__ import annotations

import pytest

from boba.toolkit.launcher import ClippedText


class TestWithinBudget:
    def test_text_survives_untouched(self) -> None:
        clipped = ClippedText.of("hello", max_bytes=10)

        assert clipped.text == "hello"
        assert clipped.total_bytes == 5
        assert not clipped.truncated

    def test_exact_budget_is_not_truncated(self) -> None:
        clipped = ClippedText.of("abcd", max_bytes=4)

        assert clipped.text == "abcd"
        assert not clipped.truncated

    def test_multibyte_counted_in_bytes(self) -> None:
        clipped = ClippedText.of("привет", max_bytes=12)

        assert clipped.text == "привет"
        assert clipped.total_bytes == 12
        assert not clipped.truncated


class TestOverBudget:
    def test_head_kept_with_notice(self) -> None:
        clipped = ClippedText.of("0123456789", max_bytes=4)

        assert clipped.text.startswith("0123")
        assert "truncated: 4 of 10 bytes shown" in clipped.text
        assert clipped.total_bytes == 10
        assert clipped.truncated

    def test_multibyte_boundary_is_not_broken(self) -> None:
        clipped = ClippedText.of("привет", max_bytes=5)

        assert clipped.text.startswith("пр")
        assert "truncated: 4 of 12 bytes shown" in clipped.text
        assert clipped.truncated


class TestBadLimits:
    def test_zero_budget(self) -> None:
        with pytest.raises(ValueError, match="max_bytes"):
            ClippedText.of("text", max_bytes=0)

    def test_negative_budget(self) -> None:
        with pytest.raises(ValueError, match="max_bytes"):
            ClippedText.of("text", max_bytes=-1)
