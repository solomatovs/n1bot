"""Общее у SQL-инструментов: лимиты секции, приведение строк, результат."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, ClassVar
from uuid import UUID

from boba.toolkit.launcher import RowStream
from boba.toolkit.result import AffectedSqlResult, ToolArtifact, render_for_llm
from boba.toolkit.sql import (
    RowPage,
    RowWindow,
    SqlLimits,
)


class FakeLimits(SqlLimits):
    """Лимиты выдуманного коннектора: секцию задаёт плагин."""

    SECTION: ClassVar[str] = "tool.fake"


class TestSqlLimits:
    def test_defaults_are_sane(self) -> None:
        limits = FakeLimits.model_validate({})
        if limits.max_rows <= 0 or limits.max_bytes <= 0:
            raise AssertionError("limits must be positive")

    def test_section_keys_are_read(self) -> None:
        limits = FakeLimits.model_validate({"max_rows": 5, "max_bytes": 100})
        if (limits.max_rows, limits.max_bytes) != (5, 100):
            raise AssertionError("section keys must reach the model")

    def test_foreign_keys_are_ignored(self) -> None:
        """В секции лежат ещё enable/tools/sandbox: модель их не касается."""
        limits = FakeLimits.model_validate({"max_rows": 5, "enable": True})
        if limits.max_rows != 5:
            raise AssertionError("extra keys must not break the model")


class TestRowStreamPlain:
    def test_row_becomes_json_safe(self) -> None:
        row: dict[str, Any] = {
            "i": 1,
            "d": Decimal("1.5"),
            "u": UUID("00000000-0000-0000-0000-000000000001"),
            "dt": date(2026, 1, 2),
            "b": b"v",
            "arr": (1, 2),
            "map": {"k": b"v"},
            "empty": None,
        }
        if not (
            RowStream.plain(row)
            == {
                "i": 1,
                "d": "1.5",
                "u": "00000000-0000-0000-0000-000000000001",
                "dt": "2026-01-02",
                "b": "v",
                "arr": [1, 2],
                "map": {"k": "v"},
                "empty": None,
            }
        ):
            raise AssertionError('RowStream.plain(row) == { "i": 1, "d": "1.5", "u": …')

    def test_non_utf8_bytes_do_not_break_the_dump(self) -> None:
        plain = RowStream.plain({"raw": b"\xff\x00ok"})
        if not (plain["raw"].endswith("ok")):
            raise AssertionError('plain["raw"].endswith("ok")')


class TestAffectedSqlResult:
    def test_status_wins_over_counter(self) -> None:
        result = AffectedSqlResult(affected_rows=5, status="DELETE 5")
        if render_for_llm(result) != "DELETE 5":
            raise AssertionError('render_for_llm(result) == "DELETE 5"')

    def test_counter_is_used_without_status(self) -> None:
        result = AffectedSqlResult(affected_rows=5, status=None)
        if render_for_llm(result) != "affected rows: 5":
            raise AssertionError('render_for_llm(result) == "affected rows: 5"')

    def test_ddl_without_counter_still_reports_success(self) -> None:
        result = AffectedSqlResult(affected_rows=None, status=None)
        if render_for_llm(result) != "statement executed":
            raise AssertionError('render_for_llm(result) == "statement executed"')
        if result.ok is not True:
            raise AssertionError("result.ok is True")

    def test_artifact_survives_serialization(self) -> None:
        result = AffectedSqlResult(affected_rows=1, status="UPDATE 1")
        revived = ToolArtifact.revive(result.model_dump(mode="json"))
        if revived != result:
            raise AssertionError("revived == result")


class TestRowWindow:
    """Окно выдачи: что пропустить, сколько отдать, где следующая страница."""

    def test_probe_asks_one_row_beyond_the_window(self) -> None:
        window = RowWindow(offset=20, max_rows=10, max_chars=1000)

        if window.probe() != 31:
            raise AssertionError(f"окно плюс разведка, дано {window.probe()}")

    def test_page_cut_by_chars_points_at_the_first_unseen_row(self) -> None:
        """Обрыв по символам сдвигает offset на показанное, а не на окно."""
        window = RowWindow(offset=0, max_rows=100, max_chars=30)

        page = RowPage(window)
        for number in range(1, 50):
            if not page.add({"n": number}):
                break

        table = page.table()
        expected = f"next offset={len(table.rows)}"

        if expected not in str(table.note):
            raise AssertionError(f"ожидалось {expected}, дано {table.note!r}")


class TestRowPage:
    """Страница: пропуск, мягкая остановка и навигация в note."""

    @staticmethod
    def _rows(count: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for number in range(1, count + 1):
            rows.append({"n": number})

        return rows

    @staticmethod
    def _filled(window: RowWindow, rows: list[dict[str, Any]]) -> RowPage:
        page = RowPage(window)
        for row in rows:
            if not page.add(row):
                break

        return page

    def test_offset_skips_and_note_points_further(self) -> None:
        window = RowWindow(offset=2, max_rows=2, max_chars=10_000)

        table = self._filled(window, self._rows(10)).table()

        if [row["n"] for row in table.rows] != [3, 4]:
            raise AssertionError(f"окно после пропуска, дано {table.rows!r}")

        if table.note != "rows 3-4; more rows available, next offset=4":
            raise AssertionError(f"навигация в note, дано {table.note!r}")

    def test_last_page_says_the_result_ended(self) -> None:
        window = RowWindow(offset=0, max_rows=10, max_chars=10_000)

        table = self._filled(window, self._rows(3)).table()

        if table.note != "rows 1-3; end of result":
            raise AssertionError(f"конец выдачи, дано {table.note!r}")

    def test_offset_past_the_end_returns_nothing(self) -> None:
        window = RowWindow(offset=50, max_rows=10, max_chars=10_000)

        table = self._filled(window, self._rows(3)).table()

        if table.rows:
            raise AssertionError("за концом выдачи строк нет")

        if table.note != "no rows at offset 50":
            raise AssertionError(f"note про пустое окно, дано {table.note!r}")

    def test_char_limit_stops_the_page_and_keeps_the_rest(self) -> None:
        """Потолок символов обрывает набор, а не роняет вызов."""
        window = RowWindow(offset=0, max_rows=100, max_chars=30)

        page = self._filled(window, self._rows(50))
        table = page.table()

        if not table.rows:
            raise AssertionError("первая строка входит всегда")

        if len(table.rows) >= 50:
            raise AssertionError("потолок символов обязан оборвать набор")

        if not page.more:
            raise AssertionError("остаток отмечен как доступный")

        if "next offset=" not in str(table.note):
            raise AssertionError(f"note зовёт за остатком, дано {table.note!r}")

    def test_single_huge_row_is_not_dropped(self) -> None:
        """Строка шире потолка всё равно отдаётся: иначе страница пуста и
        листать некуда."""
        window = RowWindow(offset=0, max_rows=10, max_chars=1)

        table = self._filled(window, [{"n": "x" * 500}]).table()

        if len(table.rows) != 1:
            raise AssertionError("одна строка приходит даже сверх потолка")
