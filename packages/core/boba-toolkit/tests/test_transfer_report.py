"""Отчёт приёмника: сколько строк легло и куда, что стало с таблицей и её
прежними строками, как движок клал строки и чем поток расходится с таблицей."""

from __future__ import annotations

from boba.toolkit.result import SqlResult
from boba.toolkit.transfer import (
    ColumnCheck,
    ColumnIssue,
    ColumnVerdict,
    DeleteOutcome,
    SchemaAction,
    TransferReport,
    Verdict,
)


def nullable(name: str, stream: str, table: str) -> ColumnCheck:
    verdict = ColumnVerdict(
        Verdict.ERROR,
        "stream is nullable, table column is not",
        ColumnIssue.NULLABLE_INTO_NOT_NULL,
    )

    return ColumnCheck(name=name, stream=stream, table=table, verdict=verdict)


def missing(name: str, table: str) -> ColumnCheck:
    verdict = ColumnVerdict(
        Verdict.ERROR, "in the table but not in the stream", ColumnIssue.NOT_IN_STREAM
    )

    return ColumnCheck(name=name, stream="", table=table, verdict=verdict)


def typed(
    name: str, stream: str, table: str, level: Verdict, message: str
) -> ColumnCheck:
    verdict = ColumnVerdict(level, message)

    return ColumnCheck(name=name, stream=stream, table=table, verdict=verdict)


class TestClickHouseLoad:
    """Загрузка из треда: таблица была, truncate через двойник, расхождения."""

    REPORT = TransferReport(
        table="default.kb_chunks",
        action=SchemaAction.KEEP,
        reason="table exists",
        backup="",
        differences=(
            nullable("chunk_id", "text", "String"),
            nullable("chunk_index", "integer", "Int32"),
            typed(
                "metadata",
                "JSON",
                "String",
                Verdict.ERROR,
                "type differs: JSON, String",
            ),
            missing("embedding", "Array(Float32)"),
            missing("tsv", "String"),
            typed(
                "tags",
                "text[]",
                "String",
                Verdict.WARNING,
                "type cannot be verified, clickhouse has no type for the stream text[]",
            ),
        ),
        deleted=DeleteOutcome(effect="replaced: the new version starts empty"),
        inserted=50235,
        method=(
            "into default.kb_chunks__ex, then exchange tables with default.kb_chunks; "
            "the previous version stays in default.kb_chunks__ex"
        ),
        transactional=False,
    )

    def markdown(self) -> str:
        result = SqlResult(engine="clickhouse", statements=self.REPORT.statements())

        return result.chat_view().markdown

    def test_caption_says_how_many_rows_went_where(self) -> None:
        assert "_50235 rows loaded into default.kb_chunks_" in self.markdown()

    def test_facts_explain_the_table_previous_rows_and_the_load(self) -> None:
        markdown = self.markdown()

        assert "**table:** `kept as is (table exists)`" in markdown
        assert "**previous rows:** `replaced: the new version starts empty`" in markdown
        assert "into default.kb_chunks__ex, then exchange tables" in markdown

    def test_same_differences_fold_into_one_row(self) -> None:
        markdown = self.markdown()

        assert "| chunk_id, chunk_index | nullable" in markdown
        assert "a NULL in the stream fails the load" in markdown
        assert markdown.count("nullable ") == 1

    def test_missing_columns_say_they_get_the_default(self) -> None:
        rows = self.REPORT.statements()[0].rows
        assert rows is not None

        missing_rows = [row for row in rows if row["stream"] == "—"]
        assert [row["columns"] for row in missing_rows] == ["embedding", "tsv"]
        assert {row["effect"] for row in missing_rows} == {
            "not in the stream: rows get the column default"
        }

    def test_errors_come_before_warnings(self) -> None:
        rows = self.REPORT.statements()[0].rows
        assert rows is not None

        assert rows[-1]["columns"] == "tags"

    def test_kept_table_note_explains_why_errors_did_not_stop_it(self) -> None:
        note = self.REPORT.statements()[0].note

        assert "differences were only reported" in note
        assert "not one transaction" in note

    def test_no_fake_sql_for_the_twin(self) -> None:
        assert "```sql" not in self.markdown()


class TestPostgresDelete:
    def test_executed_delete_is_its_own_sql_statement(self) -> None:
        report = TransferReport(
            table="public.t",
            action=SchemaAction.CREATE,
            reason="table is missing",
            backup="",
            differences=(),
            deleted=DeleteOutcome(
                effect="5 rows deleted", statement="delete from public.t where x > 1"
            ),
            inserted=10,
            method="",
            transactional=True,
        )

        loaded, deleted = report.statements()

        assert loaded.rows is None
        assert loaded.note == ""
        assert deleted.text == "delete from public.t where x > 1"
        assert deleted.status == "5 rows deleted"
