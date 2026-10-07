"""Отчёт приёмника и журнал команд вызова: сколько строк легло и куда, с
какими колонками, какие команды выполнены — и то же при падении."""

from __future__ import annotations

import logging

import pytest

from boba.toolkit.ports import ToolProgress
from boba.toolkit.result import SqlFailureResult, SqlResult
from boba.toolkit.transfer import (
    ColumnCheck,
    ColumnIssue,
    ColumnVerdict,
    CommandJournal,
    CommandKind,
    DeleteOutcome,
    LoadColumns,
    PlannedColumn,
    SchemaAction,
    SchemaCheck,
    TransferProgress,
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


def planned(name: str, field: str, type_text: str) -> PlannedColumn:
    return PlannedColumn(
        name=name, field=field, type=type_text, nullable=True, source_type="text"
    )


CHECK = SchemaCheck(
    columns=(
        nullable("chunk_id", "text", "String"),
        missing("embedding", "Array(Float32)"),
    )
)
PLANNED = (
    planned("chunk_id", "chunk_id", "Nullable(String)"),
    planned("title", "name", "Nullable(String)"),
)


class TestLoadColumns:
    ROWS = LoadColumns().rows(PLANNED, CHECK)

    def test_existing_table_type_wins_over_the_planned_one(self) -> None:
        assert self.ROWS[0]["type"] == "String"
        assert self.ROWS[1]["type"] == "Nullable(String)"

    def test_differences_and_renames_go_to_the_note(self) -> None:
        assert "a NULL would fail the load" in self.ROWS[0]["note"]
        assert self.ROWS[1]["note"] == ""
        assert self.ROWS[1]["stream field"] == "name"

    def test_table_only_columns_get_their_own_row(self) -> None:
        assert self.ROWS[2]["column"] == "embedding"
        assert self.ROWS[2]["stream field"] == "—"
        assert self.ROWS[2]["note"] == "not in the stream: rows get the column default"


class TestClickHouseLoad:
    """Загрузка из треда: таблица была, truncate через двойник."""

    REPORT = TransferReport(
        table="default.kb_chunks",
        action=SchemaAction.KEEP,
        reason="table exists",
        backup="",
        differences=tuple(CHECK.differences()),
        columns=tuple(LoadColumns().rows(PLANNED, CHECK)),
        deleted=DeleteOutcome(effect="replaced: the new version starts empty"),
        inserted=50235,
        method=(
            "into default.kb_chunks__ex, then exchange tables with default.kb_chunks; "
            "the previous version stays in default.kb_chunks__ex"
        ),
        transactional=False,
    )

    def markdown(self) -> str:
        result = SqlResult(engine="clickhouse", statements=[self.REPORT.summary()])

        return result.chat_view().markdown

    def test_caption_says_how_many_rows_went_where(self) -> None:
        assert "_50235 rows loaded into default.kb_chunks_" in self.markdown()

    def test_facts_explain_the_table_previous_rows_and_the_load(self) -> None:
        markdown = self.markdown()

        assert "**table:** `kept as is (table exists)`" in markdown
        assert "**previous rows:** `replaced: the new version starts empty`" in markdown
        assert "into default.kb_chunks__ex, then exchange tables" in markdown

    def test_columns_table_follows_the_facts(self) -> None:
        markdown = self.markdown()

        assert "| column" in markdown
        assert markdown.index("**table:**") < markdown.index("| column")

    def test_kept_table_note_explains_why_errors_did_not_stop_it(self) -> None:
        note = self.REPORT.summary().note

        assert "only reported" in note
        assert "not one transaction" in note


class TestCommandJournal:
    def test_actions_are_kept_and_lookups_only_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        journal = CommandJournal(
            "pg_stream_in", TransferProgress(ToolProgress(-1), "test")
        )

        with caplog.at_level(logging.INFO):
            with journal.command("select 1 from pg_class", CommandKind.LOOKUP) as run:
                run.status = "exists: True"

            with journal.command("truncate table t", CommandKind.ACTION) as run:
                run.status = "TRUNCATE TABLE"

        texts = [statement.text for statement in journal.statements()]
        assert texts == ["truncate table t"]
        assert journal.statements()[0].status == "TRUNCATE TABLE"

        logged = caplog.text
        assert "pg_stream_in: lookup started:\nselect 1 from pg_class" in logged
        assert "pg_stream_in: action done in" in logged

    def test_failure_shows_columns_and_the_failed_command(self) -> None:
        journal = CommandJournal(
            "ch_stream_in", TransferProgress(ToolProgress(-1), "test")
        )
        journal.columns([{"column": "id", "type": "Nullable(Int32)"}])

        with journal.command("drop table if exists t__ex", CommandKind.ACTION) as run:
            run.status = "done"

        try:
            with journal.command(
                "create table t (id Nullable(Int32)) order by id", CommandKind.ACTION
            ):
                raise RuntimeError("Sorting key contains nullable columns")
        except RuntimeError as exc:
            failure = journal.failed(exc).failure()

        assert isinstance(failure, SqlFailureResult)

        markdown = failure.chat_view().markdown
        assert "**RuntimeError: Sorting key contains nullable columns**" in markdown
        assert "| id     | Nullable(Int32) |" in markdown
        assert "```sql\ndrop table if exists t__ex\n```\n\n_done_" in markdown
        assert "create table t (id Nullable(Int32)) order by id" in markdown
        assert "_failed: RuntimeError_" in markdown

        llm = failure.llm_view()
        assert llm.startswith("RuntimeError: Sorting key contains nullable columns")
        assert "statement: create table t" in llm
