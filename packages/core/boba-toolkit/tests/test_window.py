"""Окно выдачи: пропуск, мягкая остановка и навигация в note."""

from __future__ import annotations

from typing import Any

import pytest

from boba.toolkit.window import RowPage, RowWindow


def _numbered(count: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number in range(1, count + 1):
        rows.append({"n": number})

    return rows


class TestRowWindow:
    def test_probe_asks_one_row_beyond_the_window(self) -> None:
        window = RowWindow(offset=20, limit=10)

        if window.probe() != 31:
            raise AssertionError(f"окно плюс разведка, дано {window.probe()}")

    def test_served_probe_skips_nothing_itself(self) -> None:
        window = RowWindow(offset=20, limit=10)

        if window.served_probe() != 11:
            raise AssertionError(f"окно плюс разведка, дано {window.served_probe()}")

    def test_limit_is_required_and_positive(self) -> None:
        with pytest.raises(ValueError, match="greater than or equal to 1"):
            RowWindow(offset=0, limit=0)


class TestRowPage:
    def test_offset_skips_and_note_points_further(self) -> None:
        page = RowPage(RowWindow(offset=2, limit=2), skipped=0)
        page.take(_numbered(10))

        if [row["n"] for row in page.rows] != [3, 4]:
            raise AssertionError(f"окно после пропуска, дано {page.rows!r}")

        if page.note() != "rows 3-4; more rows available, next offset=4":
            raise AssertionError(f"навигация в note, дано {page.note()!r}")

    def test_page_cut_by_limit_points_at_the_first_unseen_row(self) -> None:
        page = RowPage(RowWindow(offset=0, limit=10), skipped=0)
        page.take(_numbered(49))

        expected = f"next offset={len(page.rows)}"
        if expected not in page.note():
            raise AssertionError(f"ожидалось {expected}, дано {page.note()!r}")

    def test_last_page_says_the_result_ended(self) -> None:
        page = RowPage(RowWindow(offset=0, limit=10), skipped=0)
        page.take(_numbered(3))

        if page.more:
            raise AssertionError("данные кончились")

        if page.note() != "rows 1-3; end of result":
            raise AssertionError(f"конец выдачи, дано {page.note()!r}")

    def test_exactly_full_window_without_probe_row_ends(self) -> None:
        """Строк ровно limit: без разведочной строки продолжения нет."""
        page = RowPage(RowWindow(offset=0, limit=3), skipped=0)
        page.take(_numbered(3))

        if page.note() != "rows 1-3; end of result":
            raise AssertionError(f"конец выдачи, дано {page.note()!r}")

    def test_offset_past_the_end_returns_nothing(self) -> None:
        page = RowPage(RowWindow(offset=50, limit=10), skipped=0)
        page.take(_numbered(3))

        if page.rows:
            raise AssertionError("за концом выдачи строк нет")

        if page.note() != "no rows at offset 50":
            raise AssertionError(f"note про пустое окно, дано {page.note()!r}")

    def test_source_that_skipped_itself_is_not_skipped_again(self) -> None:
        """Сервер уже применил offset: страница берёт с первой строки, а
        навигацию считает от offset окна."""
        window = RowWindow(offset=20, limit=5)
        page = RowPage(window, skipped=window.offset)
        page.take(_numbered(window.served_probe()))

        if [row["n"] for row in page.rows] != [1, 2, 3, 4, 5]:
            raise AssertionError(f"строки сервера как есть, дано {page.rows!r}")

        if page.note() != "rows 21-25; more rows available, next offset=25":
            raise AssertionError(f"навигация от offset окна, дано {page.note()!r}")

    def test_source_cannot_skip_beyond_the_window(self) -> None:
        with pytest.raises(ValueError, match="beyond the window offset"):
            RowPage(RowWindow(offset=2, limit=5), skipped=3)

    def test_add_stops_the_stream_when_the_window_is_full(self) -> None:
        page = RowPage(RowWindow(offset=0, limit=2), skipped=0)
        taken: list[bool] = []
        for row in _numbered(3):
            taken.append(page.add(row))

        if taken != [True, True, False]:
            raise AssertionError(f"третья строка не входит, дано {taken}")

        if not page.more:
            raise AssertionError("разведочная строка показала продолжение")

    def test_single_huge_row_is_not_dropped(self) -> None:
        page = RowPage(RowWindow(offset=0, limit=10), skipped=0)
        page.take([{"n": "x" * 500}])

        if len(page.rows) != 1:
            raise AssertionError("одна строка приходит даже сверх потолка")
