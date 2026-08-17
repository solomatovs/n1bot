"""ClippedText: усечение потока по байтовому потолку с признаком усечения."""

from __future__ import annotations

import pytest

from boba.toolkit.launcher import ClippedText


class TestWithinBudget:
    def test_text_survives_untouched(self) -> None:
        clipped = ClippedText.of("hello", max_bytes=10)

        if clipped.text != "hello":
            raise AssertionError('clipped.text == "hello"')
        if clipped.total_bytes != 5:
            raise AssertionError("clipped.total_bytes == 5")
        if clipped.truncated:
            raise AssertionError("not clipped.truncated")

    def test_exact_budget_is_not_truncated(self) -> None:
        clipped = ClippedText.of("abcd", max_bytes=4)

        if clipped.text != "abcd":
            raise AssertionError('clipped.text == "abcd"')
        if clipped.truncated:
            raise AssertionError("not clipped.truncated")

    def test_multibyte_counted_in_bytes(self) -> None:
        clipped = ClippedText.of("привет", max_bytes=12)

        if clipped.text != "привет":
            raise AssertionError('clipped.text == "привет"')
        if clipped.total_bytes != 12:
            raise AssertionError("clipped.total_bytes == 12")
        if clipped.truncated:
            raise AssertionError("not clipped.truncated")


class TestOverBudget:
    def test_head_kept_with_notice(self) -> None:
        clipped = ClippedText.of("0123456789", max_bytes=4)

        if not (clipped.text.startswith("0123")):
            raise AssertionError('clipped.text.startswith("0123")')
        if "truncated: 4 of 10 bytes shown" not in clipped.text:
            raise AssertionError('"truncated: 4 of 10 bytes shown" in clipped.text')
        if clipped.total_bytes != 10:
            raise AssertionError("clipped.total_bytes == 10")
        if not (clipped.truncated):
            raise AssertionError("clipped.truncated")

    def test_multibyte_boundary_is_not_broken(self) -> None:
        clipped = ClippedText.of("привет", max_bytes=5)

        if not (clipped.text.startswith("пр")):
            raise AssertionError('clipped.text.startswith("пр")')
        if "truncated: 4 of 12 bytes shown" not in clipped.text:
            raise AssertionError('"truncated: 4 of 12 bytes shown" in clipped.text')
        if not (clipped.truncated):
            raise AssertionError("clipped.truncated")


class TestBadLimits:
    def test_zero_budget(self) -> None:
        with pytest.raises(ValueError, match="max_bytes"):
            ClippedText.of("text", max_bytes=0)

    def test_negative_budget(self) -> None:
        with pytest.raises(ValueError, match="max_bytes"):
            ClippedText.of("text", max_bytes=-1)
