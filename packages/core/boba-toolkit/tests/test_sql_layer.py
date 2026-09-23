"""Общее у SQL-инструментов: лимиты секции, приведение строк, результат."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, ClassVar
from uuid import UUID

from boba.toolkit.launcher import RowStream
from boba.toolkit.result import SqlResult, SqlStatement, ToolArtifact
from boba.toolkit.sql import SqlLimits


class FakeLimits(SqlLimits):
    """Лимиты выдуманного коннектора: секцию задаёт плагин."""

    SECTION: ClassVar[str] = "tool.fake"


class TestSqlLimits:
    def test_defaults_are_sane(self) -> None:
        limits = FakeLimits.model_validate({})
        if limits.limit <= 0 or limits.max_bytes <= 0:
            raise AssertionError("limits must be positive")

    def test_section_keys_are_read(self) -> None:
        limits = FakeLimits.model_validate({"limit": 5, "max_bytes": 100})
        if (limits.limit, limits.max_bytes) != (5, 100):
            raise AssertionError("section keys must reach the model")

    def test_foreign_keys_are_ignored(self) -> None:
        """В секции лежат ещё enable/tools/sandbox: модель их не касается."""
        limits = FakeLimits.model_validate({"limit": 5, "enable": True})
        if limits.limit != 5:
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


class TestSqlStatementCaption:
    def test_status_wins_over_counter(self) -> None:
        statement = SqlStatement(affected_rows=5, status="DELETE 5")
        if statement.caption() != "DELETE 5":
            raise AssertionError('statement.caption() == "DELETE 5"')

    def test_counter_is_used_without_status(self) -> None:
        statement = SqlStatement(affected_rows=5)
        if statement.caption() != "affected rows: 5":
            raise AssertionError('statement.caption() == "affected rows: 5"')

    def test_rows_count_without_status(self) -> None:
        statement = SqlStatement(rows=[{"a": 1}, {"a": 2}])
        if statement.caption() != "2 rows":
            raise AssertionError('statement.caption() == "2 rows"')

    def test_ddl_without_counter_still_reports_success(self) -> None:
        statement = SqlStatement()
        if statement.caption() != "statement executed":
            raise AssertionError('statement.caption() == "statement executed"')


class TestSqlResult:
    def test_single_statement_shows_rows_only(self) -> None:
        result = SqlResult(
            engine="clickhouse",
            statements=[SqlStatement(rows=[{"a": 1}], note="truncated to limit (1)")],
        )
        if result.llm_view() != '[{"a": 1}]\n\ntruncated to limit (1)':
            raise AssertionError(f"llm_view: {result.llm_view()!r}")
        if "| a" not in result.chat_view().markdown:
            raise AssertionError("markdown table in chat view")
        if "_truncated to limit (1)_" not in result.chat_view().markdown:
            raise AssertionError("note under the table")

    def test_several_statements_are_captioned(self) -> None:
        result = SqlResult(
            engine="postgres",
            statements=[
                SqlStatement(rows=[{"id": 1}], status="SELECT 1"),
                SqlStatement(affected_rows=5, status="UPDATE 5"),
            ],
        )
        if result.llm_view() != 'SELECT 1\n[{"id": 1}]\n\nUPDATE 5\nUPDATE 5':
            raise AssertionError(f"llm_view: {result.llm_view()!r}")
        markdown = result.chat_view().markdown
        if not markdown.startswith("_SELECT 1_\n\n"):
            raise AssertionError(f"caption first: {markdown!r}")
        if not markdown.endswith("_UPDATE 5_\n\n_UPDATE 5_"):
            raise AssertionError(f"status statement last: {markdown!r}")

    def test_artifact_survives_serialization(self) -> None:
        result = SqlResult(
            engine="postgres",
            statements=[SqlStatement(affected_rows=1, status="UPDATE 1")],
        )
        revived = ToolArtifact.revive(result.model_dump(mode="json"))
        if revived != result:
            raise AssertionError("revived == result")
